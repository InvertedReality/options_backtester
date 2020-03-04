from functools import reduce

import numpy as np
import pandas as pd
import pyprind

from .enums import Order, Stock, Signal, Direction, get_order
from .datahandler import HistoricalOptionsData, TiingoData
from .strategy import Strategy


class Backtest:
    """Backtest runner class."""
    def __init__(self, allocation, initial_capital=1_000_000, shares_per_contract=100):
        assert isinstance(allocation, dict)

        assets = ('stocks', 'options', 'cash')
        total_allocation = sum(allocation.get(a, 0.0) for a in assets)

        self.allocation = {}
        for asset in assets:
            self.allocation[asset] = allocation.get(asset, 0.0) / total_allocation

        self.initial_capital = initial_capital
        self.stop_if_broke = True
        self.shares_per_contract = shares_per_contract
        self._stocks = []
        self._options_strategy = None
        self._stocks_data = None
        self._options_data = None

    @property
    def stocks(self):
        return self._stocks

    @stocks.setter
    def stocks(self, stocks):
        assert all(isinstance(stock, Stock) for stock in stocks), 'Invalid stocks'
        assert np.isclose(sum(stock.percentage for stock in stocks), 1.0,
                          atol=0.000001), 'Stock percentages must sum to 1.0'
        self._stocks = list(stocks)
        return self

    @property
    def options_strategy(self):
        return self._options_strategy

    @options_strategy.setter
    def options_strategy(self, strat):
        assert isinstance(strat, Strategy)
        self._options_strategy = strat

    @property
    def stocks_data(self):
        return self._stocks_data

    @stocks_data.setter
    def stocks_data(self, data):
        assert isinstance(data, TiingoData)
        self._stocks_schema = data.schema
        self._stocks_data = data

    @property
    def options_data(self):
        return self._options_data

    @options_data.setter
    def options_data(self, data):
        assert isinstance(data, HistoricalOptionsData)
        self._options_schema = data.schema
        self._options_data = data

    def run(self, rebalance_freq=0, monthly=False, sma_days=None):
        """Runs the backtest and returns a `pd.DataFrame` of the orders executed (`self.trade_log`)

        Args:
            rebalance_freq (int, optional): Determines the frequency of portfolio rebalances. Defaults to 0.
            monthly (bool, optional):       Iterates through data monthly rather than daily. Defaults to False.

        Returns:
            pd.DataFrame:                   Log of the trades executed.
        """

        assert self._stocks_data, 'Stock data not set'
        assert all(stock.symbol in self._stocks_data['symbol'].values
                   for stock in self._stocks), 'Ensure all stocks in portfolio are present in the data'
        assert self._options_data, 'Options data not set'
        assert self._options_strategy, 'Options Strategy not set'
        assert self._options_data.schema == self._options_strategy.schema

        option_dates = self._options_data['date'].unique()
        stock_dates = self.stocks_data['date'].unique()
        assert np.array_equal(stock_dates,
                              option_dates), 'Stock and options dates do not match (check that TZ are equal)'

        self._initialize_inventories()
        self.current_cash = self.initial_capital
        self.trade_log = pd.DataFrame()
        self.balance = pd.DataFrame({
            'total capital': self.current_cash,
            'cash': self.current_cash
        },
                                    index=[self.stocks_data.start_date - pd.Timedelta(1, unit='day')])

        if sma_days:
            self.stocks_data.sma(sma_days)

        rebalancing_days = pd.date_range(
            self._stocks_data.start_date, self.stocks_data.end_date, freq=str(rebalance_freq) +
            'BMS') if rebalance_freq else []

        # Prepend the first day to the rebalancing days
        rebalancing_days = pd.DatetimeIndex([self.stocks_data.start_date]).append(rebalancing_days)

        data_iterator = self._data_iterator(monthly)
        bar = pyprind.ProgBar(len(stock_dates), bar_char='█')

        for date, stocks, options in data_iterator:
            if date in rebalancing_days:
                previous_rb_date = rebalancing_days[rebalancing_days.get_loc(date) -
                                                    1] if rebalancing_days.get_loc(date) != 0 else date
                self._update_balance(previous_rb_date, date, self._stocks_data, self._options_data)
                self._rebalance_portfolio(date, stocks, options, sma_days)

            bar.update()

        # Update balance for the period between the last rebalancing day and the last day
        self._update_balance(rebalancing_days[-1], self.stocks_data.end_date, self._stocks_data, self._options_data)

        self.balance['options capital'] = self.balance['calls capital'] + self.balance['puts capital']
        self.balance['stocks capital'] = sum(self.balance[stock.symbol] for stock in self._stocks)
        self.balance['total capital'] = self.balance['cash'].add(self.balance['stocks capital'],
                                                                 self.balance['options capital'],
                                                                 fill_value=0)
        self.balance['% change'] = self.balance['total capital'].pct_change()
        self.balance['accumulated return'] = (1.0 + self.balance['% change']).cumprod()

        return self.trade_log

    def _initialize_inventories(self):
        """Initialize empty stocks and options inventories."""
        columns = pd.MultiIndex.from_product(
            [[l.name for l in self._options_strategy.legs],
             ['contract', 'underlying', 'expiration', 'type', 'strike', 'cost', 'order']])
        totals = pd.MultiIndex.from_product([['totals'], ['cost', 'qty', 'date']])
        self._options_inventory = pd.DataFrame(columns=columns.append(totals))

        self._stocks_inventory = pd.DataFrame(columns=['symbol', 'price', 'qty'])

    def _data_iterator(self, monthly):
        """Returns combined iterator for stock and options data.
        Each step, it produces a tuple like the following:
            (date, stocks, options)

        Returns:
            generator: Daily/monthly iterator over `self._stocks_data` and `self.options_data`.
        """

        if monthly:
            it = zip(self._stocks_data.iter_months(), self._options_data.iter_months())
        else:
            it = zip(self._stocks_data.iter_dates(), self._options_data.iter_dates())

        return ((date, stocks, options) for (date, stocks), (_, options) in it)

    def _rebalance_portfolio(self, date, stocks, options, sma_days):
        """Reabalances the portfolio according to `self.allocation` weights.

        Args:
            date (pd.Timestamp):    Current date.
            stocks (pd.DataFrame):  Stocks data for the current date.
            options (pd.DataFrame): Options data for the current date.
            sma_days (int):         SMA window size
        """

        # Sell all the options currently in the inventory
        self._sell_options(options, date)

        stock_capital = self._current_stock_capital(stocks)
        total_capital = self.current_cash + stock_capital
        options_allocation = self.allocation['options'] * total_capital
        stocks_allocation = self.allocation['stocks'] * total_capital

        # Clear inventories
        self._initialize_inventories()

        self._buy_stocks(stocks, stocks_allocation, sma_days)
        self._execute_option_entries(date, options, options_allocation)

        stocks_value = sum(self._stocks_inventory['price'] * self._stocks_inventory['qty'])
        options_value = sum(self._options_inventory['totals']['cost'] * self._options_inventory['totals']['qty'])

        # Update current cash
        self.current_cash = total_capital - options_value - stocks_value

    def _sell_options(self, options, date):
        # This method essentially recycles most of the code in the filter_exits method in Strategy.
        # The whole thing needs a refactor.

        leg_candidates = self._get_current_option_quotes(options)

        for i, leg in enumerate(self._options_strategy.legs):
            fields = self._signal_fields((~leg.direction).value)
            leg_candidates[i] = leg_candidates[i].loc[:, fields.values()]
            leg_candidates[i].columns = pd.MultiIndex.from_product([["leg_{}".format(i + 1)],
                                                                    leg_candidates[i].columns])

        candidates = pd.concat(leg_candidates, axis=1)

        # If a contract is missing we replace the NaN values with those of the inventory
        # except for cost, which we imput as zero.
        imputed_inventory = self._impute_missing_option_values(self._options_inventory)
        candidates = candidates.fillna(imputed_inventory)
        total_costs = sum([candidates[l.name]['cost'] for l in self._options_strategy.legs])

        # Append the 'totals' column to candidates
        qtys = self._options_inventory['totals']['qty']
        dates = [date] * len(self._options_inventory)
        totals = pd.DataFrame.from_dict({"cost": total_costs, "qty": qtys, "date": dates})
        totals.columns = pd.MultiIndex.from_product([["totals"], totals.columns])
        candidates = pd.concat([candidates, totals], axis=1)

        exits_mask = pd.Series([True] * len(self._options_inventory))
        exits_mask.index = self._options_inventory.index

        total_costs *= candidates['totals']['qty']

        self._options_inventory.drop(self._options_inventory[exits_mask].index, inplace=True)
        self.trade_log = self.trade_log.append(candidates, ignore_index=True)
        self.current_cash -= sum(total_costs)

    def _current_stock_capital(self, stocks):
        """Return the current value of the stocks inventory.

        Args:
            stocks (pd.DataFrame): Stocks data for the current time step.

        Returns:
            float: Total capital in stocks.
        """

        current_stocks = self._stocks_inventory.merge(stocks,
                                                      how='left',
                                                      left_on='symbol',
                                                      right_on=self._stocks_schema['symbol'])
        return (current_stocks[self._stocks_schema['adjClose']] * current_stocks['qty']).sum()

    def _current_options_capital(self, options):
        # Currently unused method
        total_cost = 0.0
        for leg in self._options_strategy.legs:
            current_options = self._options_inventory[leg.name].merge(options,
                                                                      how='left',
                                                                      left_on='contract',
                                                                      right_on=self._options_schema['contract'])
            price_col = (~leg.direction).value
            try:
                cost = current_options[price_col].fillna(
                    0.0).iloc[0] * self._options_inventory['totals']['qty'].values[0] * self.shares_per_contract
                if price_col == 'bid':
                    total_cost += cost
                else:
                    total_cost -= cost
            except IndexError:
                total_cost += 0.0

        return total_cost

    def _buy_stocks(self, stocks, allocation, sma_days):
        """Buys stocks according to their given weight, optionally using an SMA entry filter.

        Args:
            stocks (pd.DataFrame):  Stocks data for the current time step.
            allocation (float):     Total capital allocation for stocks.
            sma_days (int):         SMA window.
        """

        stock_symbols = [stock.symbol for stock in self.stocks]
        query = '{} in {}'.format(self._stocks_schema['symbol'], stock_symbols)
        inventory_stocks = stocks.query(query)
        stock_percentages = np.array([stock.percentage for stock in self.stocks])
        stock_prices = inventory_stocks[self._stocks_schema['adjClose']]

        if sma_days:
            qty = np.where(inventory_stocks['sma'] < stock_prices, (allocation * stock_percentages) // stock_prices, 0)
        else:
            qty = (allocation * stock_percentages) // stock_prices

        self._stocks_inventory = pd.DataFrame({'symbol': stock_symbols, 'price': stock_prices, 'qty': qty})

    def _update_balance(self, start_date, end_date, stocks, options):
        """Updates bt.balance in batch in a certain period between rebalancing days"""
        stocks_data = stocks.query('(date >= "{}") & (date < "{}")'.format(start_date, end_date))
        options_data = options.query('(quotedate >= "{}") & (quotedate < "{}")'.format(start_date, end_date))

        calls_value = pd.Series(0, index=options_data['quotedate'].unique())
        puts_value = pd.Series(0, index=options_data['quotedate'].unique())

        for leg in self._options_strategy.legs:
            leg_inventory = self._options_inventory[leg.name]
            for contract in leg_inventory['contract']:
                leg_inventory_contract = leg_inventory.query('contract == "{}"'.format(contract))
                qty = self._options_inventory.loc[leg_inventory_contract.index]['totals']['qty'].values[0]
                current = leg_inventory_contract[['contract']].merge(options_data,
                                                                     how='left',
                                                                     left_on='contract',
                                                                     right_on='optionroot').set_index('quotedate')
                if (leg_inventory_contract['type'] == 'call').any():
                    calls_value += current[(~leg.direction).value] * qty * self.shares_per_contract
                else:
                    puts_value += current[(~leg.direction).value] * qty * self.shares_per_contract

        stocks_current = self._stocks_inventory[['symbol', 'qty']].merge(stocks_data[['date', 'symbol', 'adjClose']],
                                                                         on='symbol')
        stocks_current['cost'] = stocks_current['qty'] * stocks_current['adjClose']

        add = pd.concat([
            stocks_current[stocks_current['symbol'] == stock.symbol].set_index('date')[['cost']].rename(
                columns={'cost': stock.symbol}) for stock in self._stocks
        ],
                        axis=1)

        add['cash'] = self.current_cash
        add['options qty'] = self._options_inventory['totals']['qty'].sum()
        add['calls capital'] = calls_value
        add['puts capital'] = puts_value
        add['stocks qty'] = self._stocks_inventory['qty'].sum()

        # sort=False means we're assuming the updates are done in chronological order, i.e,
        # the dates in add are the immediate successors to the ones at the end of self.balance.
        # Pass sort=True to ensure self.balance is always sorted chronologically if needed.
        self.balance = self.balance.append(add, sort=False)

    def _execute_option_entries(self, date, options, options_allocation):
        """Enters option positions according to `self._options_strategy`.
        Calls `self._pick_entry_signals` to select from the entry signals given by the strategy.

        Args:
            date (pd.Timestamp):        Current date.
            options (pd.DataFrame):     Options data for the current time step.
            options_allocation (float): Capital amount allocated to options.
        """

        # Remove contracts already in inventory
        inventory_contracts = pd.concat(
            [self._options_inventory[leg.name]['contract'] for leg in self._options_strategy.legs])
        subset_options = options[~options[self._options_schema['contract']].isin(inventory_contracts)]

        entry_signals = []
        for leg in self._options_strategy.legs:
            flt = leg.entry_filter
            cost_field = leg.direction.value

            leg_entries = subset_options[flt(subset_options)]
            # Exit if no entry signals for the current leg
            if leg_entries.empty:
                return pd.DataFrame()

            fields = self._signal_fields(cost_field)
            leg_entries = leg_entries.reindex(columns=fields.keys())
            leg_entries.rename(columns=fields, inplace=True)

            order = get_order(leg.direction, Signal.ENTRY)
            leg_entries['order'] = order

            # Change sign of cost for SELL orders
            if leg.direction == Direction.SELL:
                leg_entries['cost'] = -leg_entries['cost']

            leg_entries['cost'] *= self.shares_per_contract
            leg_entries.columns = pd.MultiIndex.from_product([[leg.name], leg_entries.columns])
            entry_signals.append(leg_entries.reset_index(drop=True))

        # Append the 'totals' column to entry_signals
        total_costs = sum([leg_entry.droplevel(0, axis=1)['cost'] for leg_entry in entry_signals])
        qty = np.abs(options_allocation // total_costs)
        totals = pd.DataFrame.from_dict({'cost': total_costs, 'qty': qty, 'date': date})
        totals.columns = pd.MultiIndex.from_product([['totals'], totals.columns])
        entry_signals.append(totals)
        entry_signals = pd.concat(entry_signals, axis=1)

        entries = self._pick_entry_signals(entry_signals)

        # Update options inventory, trade log and current cash
        self._options_inventory = self._options_inventory.append(entries, ignore_index=True)
        self.trade_log = self.trade_log.append(entries, ignore_index=True)
        self.current_cash -= sum(total_costs)

    def _execute_option_exits(self, date, options):
        """Exits option positions according to `self._options_strategy`.
        Option positions are closed whenever the strategy signals an exit, when the profit/loss thresholds
        are exceeded or whenever the contracts in `self._options_inventory` are not found in `options`.

        Args:
            date (pd.Timestamp):        Current date.
            options (pd.DataFrame):     Options data for the current time step.
        """

        strategy = self._options_strategy
        current_options_quotes = self._get_current_option_quotes(options)

        filter_masks = []
        for i, leg in enumerate(strategy.legs):
            flt = leg.exit_filter

            # This mask is to ensure that legs with missing contracts exit.
            missing_contracts_mask = current_options_quotes[i]['cost'].isna()

            filter_masks.append(flt(current_options_quotes[i]) | missing_contracts_mask)
            fields = self._signal_fields((~leg.direction).value)
            current_options_quotes[i] = current_options_quotes[i].reindex(columns=fields.keys())
            current_options_quotes[i].rename(columns=fields, inplace=True)
            current_options_quotes[i].columns = pd.MultiIndex.from_product([[leg.name],
                                                                            current_options_quotes[i].columns])

        exit_candidates = pd.concat(current_options_quotes, axis=1)

        # If a contract is missing we replace the NaN values with those of the inventory
        # except for cost, which we imput as zero.
        exit_candidates = self._impute_missing_option_values(exit_candidates)

        # Append the 'totals' column to exit_candidates
        qtys = self._options_inventory['totals']['qty']
        total_costs = sum([exit_candidates[l.name]['cost'] for l in self.legs])
        totals = pd.DataFrame.from_dict({'cost': total_costs, 'qty': qtys, 'date': date})
        totals.columns = pd.MultiIndex.from_product([['totals'], totals.columns])
        exit_candidates = pd.concat([exit_candidates, totals], axis=1)

        # Compute which contracts need to exit, either because of price thresholds or user exit filters
        threshold_exits = strategy.filter_thresholds(self._options_inventory['totals']['cost'], total_costs)
        filter_mask = reduce(lambda x, y: x | y, filter_masks)
        exits_mask = threshold_exits | filter_mask

        exits = exit_candidates[exits_mask]
        total_costs = total_costs[exits_mask] * exits['totals']['qty']

        # Update options inventory, trade log and current cash
        self._options_inventory.drop(self._options_inventory[exits_mask].index, inplace=True)
        self.trade_log = self.trade_log.append(exits, ignore_index=True)
        self.current_cash -= sum(total_costs)

    def _pick_entry_signals(self, entry_signals):
        """Returns the entry signals to execute.

        Args:
            entry_signals (pd.DataFrame):   DataFrame of option entry signals chosen by the strategy.

        Returns:
            pd.DataFrame:                   DataFrame of entries to execute.
        """

        if not entry_signals.empty:
            # FIXME: This is a naive signal selection criterion, it simply picks the first one in `entry_singals`
            return entry_signals.iloc[0]
        else:
            return entry_signals

    def _signal_fields(self, cost_field):
        fields = {
            self._options_schema['contract']: 'contract',
            self._options_schema['underlying']: 'underlying',
            self._options_schema['expiration']: 'expiration',
            self._options_schema['type']: 'type',
            self._options_schema['strike']: 'strike',
            self._options_schema[cost_field]: 'cost',
            'order': 'order'
        }

        return fields

    def _get_current_option_quotes(self, options):
        """Returns the current quotes for all the options in `self._options_inventory` as a list of DataFrames.
        It also adds a `cost` column with the cost of closing the position in each contract and an `order`
        column with the corresponding exit order type.

        Args:
            options (pd.DataFrame): Options data in the current time step.

        Returns:
            [pd.DataFrame]:         List of DataFrames, one for each leg in `self._options_inventory`,
                                    with the exit cost for the contracts.
        """

        current_options_quotes = []
        for leg in self._options_strategy.legs:
            inventory_leg = self._options_inventory[leg.name]

            # This is a left join to ensure that the result has the same length as the inventory. If the contract
            # isn't in the daily data the values will all be NaN and the filters should all yield False.
            leg_options = inventory_leg[['contract']].merge(options,
                                                            how='left',
                                                            left_on='contract',
                                                            right_on=leg.schema['contract'])

            # leg_options.index needs to be the same as the inventory's so that the exit masks that are constructed
            # from it can be correctly applied to the inventory.
            leg_options.index = self._options_inventory.index
            leg_options['order'] = get_order(leg.direction, Signal.EXIT)
            leg_options['cost'] = leg_options[self._options_schema[(~leg.direction).value]]

            # Change sign of cost for SELL orders
            if ~leg.direction == Direction.SELL:
                leg_options['cost'] = -leg_options['cost']
            leg_options['cost'] *= self.shares_per_contract

            current_options_quotes.append(leg_options)

        return current_options_quotes

    def _impute_missing_option_values(self, exit_candidates):
        """Returns a copy of the inventory with the cost of all its contracts set to zero.

        Args:
            exit_candidates (pd.DataFrame): DataFrame of exit candidates with possible missing values.

        Returns:
            pd.DataFrame:                   Exit candidates with imputed values.
        """
        df = self._options_inventory.copy()
        for leg in self._options_strategy.legs:
            df.at[:, (leg.name, 'cost')] = 0

        return exit_candidates.fillna(df)

    def __repr__(self):
        return "Backtest(capital={}, allocation={}, stocks={}, strategy={})".format(
            self.current_cash, self.allocation, self._stocks, self._options_strategy)
