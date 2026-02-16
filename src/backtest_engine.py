"""
Backtest Engine
===============
Core backtest replay logic for 1H trend-following strategy.

Provides:
- Price replay from historical data
- Realistic trade simulation (spread, missed fills, fees)
- Train/test split evaluation
- Performance metrics computation
"""

import random
import os
import csv
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from src.backtest_shared import (
    create_strategy_state,
    update_strategy_state,
    check_entry_signal,
    check_exit_conditions,
    compute_ma,
    parse_time_left
)


@dataclass
class BacktestTrade:
    """Record of a backtest trade."""
    market_id: str
    token_id: str
    outcome: str
    entry_time: float
    entry_price: float
    exit_time: Optional[float] = None
    exit_price: Optional[float] = None
    pnl_cents: float = 0.0
    reason: str = ""
    train_test: str = "TRAIN"
    spread_cost: float = 0.0
    fee_cost: float = 0.0
    missed_fill: bool = False


@dataclass
class BacktestState:
    """Backtest state for one market."""
    prices: List[float] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)
    ma_prices: List[float] = field(default_factory=list)
    entry: Optional[Dict] = None
    last_cooldown: float = 0


class BacktestEngine:
    """Backtest engine for 1H trend-following strategy."""
    
    def __init__(self, config: Dict, markets: List[Dict]):
        self.config = config
        self.markets = markets
        
        # Train/test split
        test_split = config.get('BACKTEST_TEST_SPLIT', 0.2)
        split_idx = int(len(markets) * (1 - test_split))
        
        # Sort markets by end_date for proper temporal split
        sorted_markets = sorted(markets, key=lambda m: m.get('end_date', ''))
        
        self.train_markets = sorted_markets[:split_idx]
        self.test_markets = sorted_markets[split_idx:]
        
        # Random for missed fills (deterministic)
        seed = config.get('BACKTEST_RANDOM_SEED', 42)
        self.rng = random.Random(seed)
        
        # Realism parameters
        self.cost_per_side_cents = config.get('BACKTEST_COST_PER_SIDE_CENTS', 2) / 100
        self.missed_fill_prob = config.get('BACKTEST_MISSED_FILL_PROBABILITY', 0.15)
        self.fee_bps = config.get('BACKTEST_FEE_BPS', 0)
        
        # Initial balance
        self.initial_balance = config.get('BACKTEST_INITIAL_BALANCE', 100.0)
        
        # Results storage
        self.train_trades: List[BacktestTrade] = []
        self.test_trades: List[BacktestTrade] = []
        self.equity_curve: List[Dict] = []
        
        # Strategy state per market (keyed by condition_id)
        self.strategy_states: Dict[str, BacktestState] = {}
        
        # Decision log
        self.decisions: List[Dict] = []
        
    def run(self) -> Dict:
        """Run backtest on train and test sets."""
        print(f"\n{'='*60}")
        print("BACKTEST ENGINE: 1H Trend-Following")
        print(f"{'='*60}")
        print(f"Total markets: {len(self.markets)}")
        print(f"Train markets: {len(self.train_markets)}")
        print(f"Test markets: {len(self.test_markets)}")
        print(f"Initial balance: ${self.initial_balance:.2f}")
        print(f"Cost per side: {self.cost_per_side_cents * 100:.1f}c")
        print(f"Missed fill probability: {self.missed_fill_prob * 100:.0f}%")
        print(f"Fee (BPS): {self.fee_bps}")
        print(f"Random seed: {self.config.get('BACKTEST_RANDOM_SEED', 42)}")
        print(f"{'='*60}\n")
        
        # Run on train
        print("Running TRAIN split...")
        train_results = self._run_split(self.train_markets, "TRAIN")
        
        # Run on test
        print("\nRunning TEST split...")
        test_results = self._run_split(self.test_markets, "TEST")
        
        # Combined results
        combined = self._combine_results(train_results, test_results)
        
        return {
            "train": train_results,
            "test": test_results,
            "combined": combined,
            "config": {
                "lookback_days": self.config.get('BACKTEST_LOOKBACK_DAYS', 365),
                "test_split": self.config.get('BACKTEST_TEST_SPLIT', 0.2),
                "random_seed": self.config.get('BACKTEST_RANDOM_SEED', 42),
                "cost_per_side_cents": self.config.get('BACKTEST_COST_PER_SIDE_CENTS', 2),
                "missed_fill_probability": self.config.get('BACKTEST_MISSED_FILL_PROBABILITY', 0.15)
            }
        }
    
    def _run_split(self, markets: List[Dict], label: str) -> Dict:
        """Run backtest on a market split."""
        # Reset equity for this split
        equity = self.initial_balance
        
        # Track trades for this split
        split_trades: List[BacktestTrade] = []
        
        # Reset strategy states
        self.strategy_states = {}
        
        for i, market in enumerate(markets):
            if (i + 1) % 10 == 0:
                print(f"   Processing market {i+1}/{len(markets)}...")
            
            # Get timeseries data
            yes_prices = market.get('yes_prices', [])
            no_prices = market.get('no_prices', [])
            
            if not yes_prices or not no_prices:
                continue
            
            # Merge prices by timestamp
            merged = self._merge_prices(yes_prices, no_prices)
            if not merged:
                continue
            
            # Simulate this market
            market_equity, market_trades = self._simulate_market(
                market, merged, label, equity
            )
            
            split_trades.extend(market_trades)
            equity = market_equity
        
        # Compute metrics
        metrics = self._compute_metrics(split_trades, label)
        
        # Print summary
        print(f"\n{label} RESULTS:")
        print(f"  P&L: ${metrics['total_pnl']:.2f}")
        print(f"  Win Rate: {metrics['win_rate']*100:.1f}%")
        print(f"  Avg Win: ${metrics['avg_win']:.2f}")
        print(f"  Avg Loss: ${metrics['avg_loss']:.2f}")
        print(f"  Trades: {metrics['num_trades']}")
        
        if label == "TRAIN":
            self.train_trades = split_trades
        else:
            self.test_trades = split_trades
        
        return metrics
    
    def _merge_prices(
        self,
        yes_prices: List[Dict],
        no_prices: List[Dict]
    ) -> List[Tuple[float, float, float]]:
        """Merge YES and NO prices by timestamp.
        
        Returns:
            List of (timestamp, yes_price, no_price)
        """
        # Create lookup by timestamp
        yes_by_ts = {p['timestamp']: p['price'] for p in yes_prices}
        no_by_ts = {p['timestamp']: p['price'] for p in no_prices}
        
        # Get all timestamps
        all_ts = sorted(set(yes_by_ts.keys()) & set(no_by_ts.keys()))
        
        merged = []
        for ts in all_ts:
            yes_p = yes_by_ts.get(ts, 0)
            no_p = no_by_ts.get(ts, 0)
            if yes_p > 0 and no_p > 0:
                merged.append((ts, yes_p, no_p))
        
        return merged
    
    def _simulate_market(
        self,
        market: Dict,
        merged_prices: List[Tuple[float, float, float]],
        label: str,
        starting_equity: float
    ) -> Tuple[float, List[BacktestTrade]]:
        """Simulate trading on one market's price history.
        
        Args:
            market: Market dictionary
            merged_prices: List of (timestamp, yes_price, no_price)
            label: "TRAIN" or "TEST"
            starting_equity: Starting equity for this market
        
        Returns:
            Tuple of (final_equity, list of trades)
        """
        condition_id = market.get('condition_id', '')
        yes_token = market.get('yes_token_id', '')
        no_token = market.get('no_token_id', '')
        end_date = market.get('end_date', '')
        title = market.get('question', '')
        
        # Get trade size
        trade_size = self.config.get('MOMENTUM_SIZE', 5.0)
        
        # Strategy state for this market
        if condition_id not in self.strategy_states:
            self.strategy_states[condition_id] = BacktestState()
        
        state = self.strategy_states[condition_id]
        
        trades = []
        equity = starting_equity
        position = None  # Current open position
        
        for ts, yes_price, no_price in merged_prices:
            # Update YES state
            state.prices.append(yes_price)
            state.timestamps.append(ts)
            state.ma_prices.append(yes_price)
            
            # Keep buffers bounded
            max_buffer = 1000
            if len(state.prices) > max_buffer:
                state.prices = state.prices[-max_buffer:]
                state.timestamps = state.timestamps[-max_buffer:]
                state.ma_prices = state.ma_prices[-max_buffer:]
            
            # Update NO state (we track both)
            no_state_key = f"{condition_id}_NO"
            if no_state_key not in self.strategy_states:
                self.strategy_states[no_state_key] = BacktestState()
            no_state = self.strategy_states[no_state_key]
            no_state.prices.append(no_price)
            no_state.timestamps.append(ts)
            no_state.ma_prices.append(no_price)
            
            # Keep buffers bounded
            max_buffer = 1000
            if len(state.prices) > max_buffer:
                state.prices = state.prices[-max_buffer:]
                state.timestamps = state.timestamps[-max_buffer:]
                state.ma_prices = state.ma_prices[-max_buffer:]
            if len(no_state.prices) > max_buffer:
                no_state.prices = no_state.prices[-max_buffer:]
                no_state.timestamps = no_state.timestamps[-max_buffer:]
                no_state.ma_prices = no_state.ma_prices[-max_buffer:]
            
            # If we have a position, check exit conditions
            if position:
                outcome = position['outcome']
                entry_price = position['entry_price']
                entry_time = position['entry_time']
                
                # Current price depends on outcome
                current_price = yes_price if outcome == "YES" else no_price
                
                # Get MA
                ma_prices = state.ma_prices if outcome == "YES" else no_state.ma_prices
                ma_value = compute_ma(ma_prices, self.config.get('TREND_TRAILING_MA_PERIODS', 20))
                
                # Check exit
                should_exit, reason, pnl_ticks = check_exit_conditions(
                    entry_price, current_price, entry_time, ts,
                    outcome, ma_value, self.config
                )
                
                if should_exit:
                    # Apply exit spread penalty
                    exit_price = current_price - self.cost_per_side_cents
                    
                    # Calculate PnL
                    if outcome == "YES":
                        pnl = (exit_price - entry_price) * trade_size
                    else:
                        pnl = (entry_price - exit_price) * trade_size
                    
                    # Apply fee
                    fee = exit_price * trade_size * (self.fee_bps / 10000)
                    pnl -= fee
                    
                    # Record trade
                    trade = BacktestTrade(
                        market_id=condition_id,
                        token_id=yes_token if outcome == "YES" else no_token,
                        outcome=outcome,
                        entry_time=entry_time,
                        entry_price=entry_price,
                        exit_time=ts,
                        exit_price=exit_price,
                        pnl_cents=pnl * 100,
                        reason=reason,
                        train_test=label,
                        spread_cost=self.cost_per_side_cents * trade_size,
                        fee_cost=fee
                    )
                    trades.append(trade)
                    equity += pnl
                    position = None
            
            # Check for entry (if no position)
            if not position:
                # Check cooldown
                cooldown_minutes = self.config.get('TREND_COOLDOWN_MINUTES', 30)
                if ts - state.last_cooldown < cooldown_minutes * 60:
                    continue
                
                # Try YES entry
                yes_state = {
                    'prices': state.prices,
                    'timestamps': state.timestamps
                }
                market_info = {
                    'condition_id': condition_id,
                    'outcome': 'YES',
                    'end_date': end_date
                }
                
                signal = check_entry_signal(yes_state, market_info, ts, self.config)
                
                if signal:
                    # Apply missed fill probability
                    if self.rng.random() < self.missed_fill_prob:
                        # Missed fill - log but don't trade
                        self._log_decision(
                            "SKIP", market, 'YES', ts, yes_price,
                            signal.get('trendiness', 0), signal.get('breakout', 'N/A'),
                            signal.get('time_left'), signal.get('confidence', 0),
                            "MISSED_FILL", label
                        )
                    else:
                        # Execute entry
                        entry_price = yes_price + self.cost_per_side_cents
                        position = {
                            'outcome': 'YES',
                            'entry_price': entry_price,
                            'entry_time': ts,
                            'token_id': yes_token
                        }
                        state.last_cooldown = ts
                        
                        self._log_decision(
                            "ENTER_YES", market, 'YES', ts, entry_price,
                            signal.get('trendiness', 0), signal.get('breakout', 'N/A'),
                            signal.get('time_left'), signal.get('confidence', 0),
                            f"ENTRY:Conf={signal.get('confidence', 0):.2f}", label
                        )
                
                # Try NO entry (if no YES entry)
                if not position:
                    no_state_dict = {
                        'prices': no_state.prices,
                        'timestamps': no_state.timestamps
                    }
                    market_info_no = {
                        'condition_id': condition_id,
                        'outcome': 'NO',
                        'end_date': end_date
                    }
                    
                    signal = check_entry_signal(no_state_dict, market_info_no, ts, self.config)
                    
                    if signal:
                        # Apply missed fill probability
                        if self.rng.random() < self.missed_fill_prob:
                            self._log_decision(
                                "SKIP", market, 'NO', ts, no_price,
                                signal.get('trendiness', 0), signal.get('breakout', 'N/A'),
                                signal.get('time_left'), signal.get('confidence', 0),
                                "MISSED_FILL", label
                            )
                        else:
                            # Execute entry
                            entry_price = no_price + self.cost_per_side_cents
                            position = {
                                'outcome': 'NO',
                                'entry_price': entry_price,
                                'entry_time': ts,
                                'token_id': no_token
                            }
                            no_state.last_cooldown = ts
                            
                            self._log_decision(
                                "ENTER_NO", market, 'NO', ts, entry_price,
                                signal.get('trendiness', 0), signal.get('breakout', 'N/A'),
                                signal.get('time_left'), signal.get('confidence', 0),
                                f"ENTRY:Conf={signal.get('confidence', 0):.2f}", label
                            )
        
        # Force close any remaining position at end
        if position:
            ts = state.timestamps[-1] if state.timestamps else 0
            yes_price = state.prices[-1] if state.prices else 0.5
            no_price = no_state.prices[-1] if no_state.prices else 0.5
            
            outcome = position['outcome']
            entry_price = position['entry_price']
            current_price = yes_price if outcome == "YES" else no_price
            
            # Apply exit spread
            exit_price = current_price - self.cost_per_side_cents
            
            if outcome == "YES":
                pnl = (exit_price - entry_price) * trade_size
            else:
                pnl = (entry_price - exit_price) * trade_size
            
            trade = BacktestTrade(
                market_id=condition_id,
                token_id=position['token_id'],
                outcome=outcome,
                entry_time=position['entry_time'],
                entry_price=entry_price,
                exit_time=ts,
                exit_price=exit_price,
                pnl_cents=pnl * 100,
                reason="END_OF_DATA",
                train_test=label,
                spread_cost=self.cost_per_side_cents * trade_size
            )
            trades.append(trade)
            equity += pnl
        
        return equity, trades
    
    def _log_decision(
        self,
        action: str,
        market: Dict,
        outcome: str,
        timestamp: float,
        price: float,
        trendiness: float,
        breakout: str,
        time_left: Optional[float],
        confidence: float,
        reason: str,
        train_test: str
    ):
        """Log a trade decision."""
        self.decisions.append({
            'timestamp': timestamp,
            'datetime': datetime.fromtimestamp(timestamp).isoformat() if timestamp else '',
            'action': action,
            'market_id': market.get('condition_id', ''),
            'market_title': market.get('question', '')[:50],
            'outcome': outcome,
            'price': price,
            'trendiness': trendiness,
            'breakout': breakout,
            'time_left': time_left,
            'confidence': confidence,
            'reason': reason,
            'train_test': train_test
        })
    
    def _compute_metrics(self, trades: List[BacktestTrade], label: str) -> Dict:
        """Compute performance metrics."""
        if not trades:
            return {
                'total_pnl': 0.0,
                'win_rate': 0.0,
                'avg_win': 0.0,
                'avg_loss': 0.0,
                'trades_per_day': 0.0,
                'max_drawdown': 0.0,
                'num_trades': 0,
                'num_markets': len(self.markets)
            }
        
        pnls = [t.pnl_cents / 100 for t in trades]  # Convert to dollars
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        
        num_trades = len(trades)
        total_pnl = sum(pnls)
        
        # Win rate
        win_rate = len(wins) / num_trades if num_trades > 0 else 0
        
        # Average win/loss
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        
        # Trades per day (estimate from time range)
        if trades:
            time_span = (trades[-1].exit_time - trades[0].entry_time) / (24 * 3600)
            trades_per_day = num_trades / max(1, time_span)
        else:
            trades_per_day = 0
        
        # Max drawdown
        equity = self.initial_balance
        peak = equity
        max_dd = 0
        for pnl in pnls:
            equity += pnl
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
        
        return {
            'total_pnl': total_pnl,
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'trades_per_day': trades_per_day,
            'max_drawdown': -max_dd,
            'num_trades': num_trades,
            'num_markets': len(self.markets)
        }
    
    def _combine_results(self, train: Dict, test: Dict) -> Dict:
        """Combine train and test results."""
        total_trades = train['num_trades'] + test['num_trades']
        
        # Weighted average for combined metrics
        if total_trades > 0:
            combined_pnl = train['total_pnl'] + test['total_pnl']
            
            # Combined win rate
            train_wins = int(train['win_rate'] * train['num_trades'])
            test_wins = int(test['win_rate'] * test['num_trades'])
            combined_win_rate = (train_wins + test_wins) / total_trades
            
            # Combined trades per day
            combined_trades_per_day = train['trades_per_day'] + test['trades_per_day']
            
            # Combined max drawdown
            combined_max_dd = max(train['max_drawdown'], test['max_drawdown'])
        else:
            combined_pnl = 0
            combined_win_rate = 0
            combined_trades_per_day = 0
            combined_max_dd = 0
        
        return {
            'total_pnl': combined_pnl,
            'win_rate': combined_win_rate,
            'avg_win': train['avg_win'],  # Use train as reference
            'avg_loss': train['avg_loss'],
            'trades_per_day': combined_trades_per_day,
            'max_drawdown': combined_max_dd,
            'num_trades': total_trades,
            'num_markets': train['num_markets'] + test['num_markets']
        }
    
    def save_outputs(self, output_dir: str = "data/backtest_outputs"):
        """Save backtest outputs to files."""
        os.makedirs(output_dir, exist_ok=True)
        
        # Summary JSON
        results = {
            'train': {
                'total_pnl': sum(t.pnl_cents / 100 for t in self.train_trades),
                'win_rate': len([t for t in self.train_trades if t.pnl_cents > 0]) / max(1, len(self.train_trades)),
                'num_trades': len(self.train_trades)
            },
            'test': {
                'total_pnl': sum(t.pnl_cents / 100 for t in self.test_trades),
                'win_rate': len([t for t in self.test_trades if t.pnl_cents > 0]) / max(1, len(self.test_trades)),
                'num_trades': len(self.test_trades)
            }
        }
        
        with open(f"{output_dir}/summary.json", 'w') as f:
            json.dump(results, f, indent=2)
        
        # Trades CSV
        all_trades = self.train_trades + self.test_trades
        with open(f"{output_dir}/trades.csv", 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'market_id', 'token_id', 'outcome', 'entry_time', 'entry_price',
                'exit_time', 'exit_price', 'pnl_cents', 'reason', 'train_test',
                'spread_cost', 'fee_cost', 'missed_fill'
            ])
            for t in all_trades:
                writer.writerow([
                    t.market_id, t.token_id, t.outcome, t.entry_time, t.entry_price,
                    t.exit_time, t.exit_price, t.pnl_cents, t.reason, t.train_test,
                    t.spread_cost, t.fee_cost, t.missed_fill
                ])
        
        # Decisions CSV
        with open(f"{output_dir}/decisions.csv", 'w', newline='') as f:
            if self.decisions:
                writer = csv.DictWriter(f, fieldnames=self.decisions[0].keys())
                writer.writeheader()
                writer.writerows(self.decisions)
        
        print(f"\nOutputs saved to {output_dir}/")
        print(f"  - summary.json")
        print(f"  - trades.csv ({len(all_trades)} trades)")
        print(f"  - decisions.csv ({len(self.decisions)} decisions)")
