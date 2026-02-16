# v14 Production Validation Checklist

## Pre-Launch Validation (Before Starting Bot)

This checklist ensures the v14 production hardening features are working correctly before running the bot in paper trading mode.

---

## 1. Environment Setup

### Python Version
```bash
python3 --version
# Expected: Python 3.9.6 or higher
```

### Dependencies
```bash
pip3 list | grep -E 'py-clob-client|web3|requests'
# Expected: All installed
```

### Virtual Environment (if using)
```bash
source venv/bin/activate
# Should activate without errors
```

---

## 2. Configuration Validation

### Config File Exists
```bash
ls -la config/config.json
# Expected: File exists with CONFIG_VERSION = 14
```

### Config Version Check
```bash
python3 -c "import json; print(json.load(open('config/config.json'))['_config_version'])"
# Expected: 14
```

### Required Config Fields (v14)
```bash
python3 validate_config.py
# Will check all required v14 fields exist
```

---

## 3. State File Validation

### State Directories Exist
```bash
mkdir -p data/metrics data/parity_reports
ls -la data/
# Expected: Directories exist
```

### Backup Rotation Test
```bash
python3 -c "
from src.state_backup import save_state_with_backup, load_state_with_recovery
import os
test_data = {'version': 1, 'test': 'data'}
save_state_with_backup('data/test_backup.json', test_data, generations=5)
loaded = load_state_with_recovery('data/test_backup.json', required_keys=['version', 'test'])
assert loaded == test_data, 'Backup/recovery failed'
print('✅ Backup rotation works')
os.remove('data/test_backup.json')
"
# Expected: ✅ Backup rotation works
```

---

## 4. Module Import Test

### All New Modules Import Successfully
```bash
python3 -c "
from src.metrics_logger import MetricsLogger
from src.parity_checker import ParityChecker
from src.health_monitor import HealthMonitor
from src.state_backup import save_state_with_backup, load_state_with_recovery
print('✅ All v14 modules import successfully')
"
# Expected: ✅ All v14 modules import successfully
```

---

## 5. Blockchain Monitor Validation

### WebSocket URL Configured (if enabled)
```bash
python3 -c "
import json
config = json.load(open('config/config.json'))
if config.get('USE_BLOCKCHAIN_MONITOR'):
    assert config.get('POLYGON_RPC_WSS'), 'POLYGON_RPC_WSS not set'
    assert config['POLYGON_RPC_WSS'].startswith('wss://'), 'Invalid WSS URL'
    print('✅ Blockchain monitor configured')
else:
    print('⏭️  Blockchain monitor disabled')
"
# Expected: ✅ Blockchain monitor configured OR ⏭️  Blockchain monitor disabled
```

### Blockchain Monitor Connectivity Test
```bash
python3 -c "
import json
config = json.load(open('config/config.json'))
if config.get('USE_BLOCKCHAIN_MONITOR') and config.get('POLYGON_RPC_WSS'):
    from web3 import Web3
    w3 = Web3(Web3.WebsocketProvider(config['POLYGON_RPC_WSS']))
    if w3.is_connected():
        print(f'✅ Blockchain RPC connected (block: {w3.eth.block_number})')
    else:
        print('❌ Blockchain RPC connection failed')
else:
    print('⏭️  Blockchain monitor disabled')
"
# Expected: ✅ Blockchain RPC connected (block: XXXXX)
```

---

## 6. Fee Classification Test

### Fee Tier Classification
```bash
python3 -c "
from src.market import MarketDataService
config = {'MODE': 'PAPER'}
mds = MarketDataService(config)

# Test crypto market
crypto_bps = mds._classify_fee_tier('Bitcoin: Up or Down by 10:00AM ET')
assert crypto_bps == 1000, f'Crypto fee wrong: {crypto_bps}'

# Test sports market
sports_bps = mds._classify_fee_tier('Team A vs Team B - Match Winner')
assert sports_bps == 0, f'Sports fee wrong: {sports_bps}'

# Test unknown market
unknown_bps = mds._classify_fee_tier('Some Unknown Market')
assert unknown_bps is None, f'Unknown fee should be None: {unknown_bps}'

print('✅ Fee classification works correctly')
"
# Expected: ✅ Fee classification works correctly
```

---

## 7. Signal Deduplication Test

### Global Dedup Logic
```bash
python3 -c "
from src.whale_tracker import WhaleTracker
import time

config = {'MODE': 'PAPER'}
wt = WhaleTracker(config)

# Test blockchain signal dedup
signal1 = {
    'source': 'blockchain',
    'tx_hash': '0xtest123',
    'log_index': 0,
}
signal2 = {
    'source': 'blockchain',
    'tx_hash': '0xtest123',
    'log_index': 0,
}

assert not wt.is_duplicate_signal(signal1), 'First signal should not be duplicate'
assert wt.is_duplicate_signal(signal2), 'Second signal should be duplicate'

print('✅ Signal deduplication works')
"
# Expected: ✅ Signal deduplication works
```

---

## 8. Health Monitor Test

### Health Monitor Initialization
```bash
python3 -c "
from src.health_monitor import HealthMonitor
config = {
    'HEALTH_MONITOR_ENABLED': True,
    'HEALTH_CHECK_INTERVAL_SEC': 30,
}
hm = HealthMonitor(config)
hm.update_main_loop_heartbeat()
hm.update_blockchain_block(12345678)
status = hm.get_health_status()
assert status['overall_status'] == 'HEALTHY', f'Expected HEALTHY, got {status[\"overall_status\"]}'
hm.stop()
print('✅ Health monitor works')
"
# Expected: ✅ Health monitor works
```

---

## 9. Metrics Logger Test

### Metrics Collection and Flush
```bash
python3 -c "
from src.metrics_logger import MetricsLogger
import time
import os

config = {
    'METRICS_LOGGING_ENABLED': True,
    'METRICS_LOG_INTERVAL_SEC': 1,
}
ml = MetricsLogger(config)
ml.increment('test_counter', 5)
ml.set_gauge('test_gauge', 100)
with ml.timer('test_timing'):
    time.sleep(0.01)

# Wait for flush
time.sleep(2)

# Check that metrics file was created
date_str = time.strftime('%Y-%m-%d')
assert os.path.exists(f'data/metrics/metrics_{date_str}.csv'), 'Metrics CSV not created'
assert os.path.exists(f'data/metrics/metrics_{date_str}.jsonl'), 'Metrics JSONL not created'

ml.stop()
print('✅ Metrics logger works')
"
# Expected: ✅ Metrics logger works
```

---

## 10. Parity Checker Test

### Parity Matching Logic
```bash
python3 -c "
from src.parity_checker import ParityChecker
import time

config = {'PARITY_CHECK_ENABLED': True}
pc = ParityChecker(config)

# Record blockchain event
bc_event = {
    'tx_hash': '0xtest456',
    'condition_id': '0xcondition123',
    'outcome': 'YES',
    'whale_address': '0xwhale',
    'whale_price': 0.55,
    'whale_side': 'BUY',
    'size': 100.0,
    'timestamp': time.time(),
}
pc.record_blockchain_event(bc_event)

# Record matching API trade
api_trade = {
    'tx_hash': '0xtest456',
    'condition_id': '0xcondition123',
    'outcome': 'YES',
    'wallet': '0xwhale',
    'price': 0.55,
    'side': 'BUY',
    'size': 100.0,
    'timestamp': time.time(),
}
pc.record_api_trade(api_trade)

# Run matching
pc.run_matching()

# Check stats
assert pc.stats['total_matched'] == 1, f'Expected 1 match, got {pc.stats[\"total_matched\"]}'
print('✅ Parity checker works')
"
# Expected: ✅ Parity checker works
```

---

## 11. Integration Test (Bot Startup)

### Dry Run (15 seconds)
```bash
timeout 15 python3 run.py || true
# Expected: Bot starts, initializes all systems, runs for 15s without crashes
# Check output for:
# - [*] Bot warming up...
# - [*] Found N active markets
# - [BLOCKCHAIN] Real-time monitoring started (if enabled)
# - [METRICS] Logger started
# - [HEALTH] Monitor started
# - [PARITY] Checker initialized
```

### Check State Files Created
```bash
ls -la data/
# Expected: paper_state.json, risk_state.json, whale_state.json, wallet_scores.json
# Expected: metrics/, parity_reports/
```

---

## 12. Post-Launch Monitoring

### First Hour Checklist
- [ ] Bot runs without crashes for 1+ hour
- [ ] Metrics CSV files created in `data/metrics/`
- [ ] Health monitor shows HEALTHY status
- [ ] Blockchain monitor connected (if enabled)
- [ ] No duplicate trades in paper_state.json
- [ ] Parity checker accumulating matches (if blockchain enabled)

### First 24 Hours Checklist
- [ ] Daily parity report generated in `data/parity_reports/`
- [ ] Match rate >90% (if blockchain enabled)
- [ ] Side error rate <2% (if blockchain enabled)
- [ ] No state file corruption (all .json files valid)
- [ ] Backup files (.bak1, .bak2, etc.) created
- [ ] Dashboard accessible and showing live data

### Shadow Mode Validation (2 weeks)
After 2 weeks of clean paper trading:
- [ ] >95% parity match rate
- [ ] <1% side error rate
- [ ] Zero state corruption incidents
- [ ] Zero emergency state saves
- [ ] Blockchain monitor uptime >99%
- [ ] All copy trades executed within 5 seconds of signal
- [ ] Paper PnL shows consistent profitability (realistic/pessimistic scenarios)

---

## Troubleshooting

### Bot Won't Start
1. Check Python version: `python3 --version`
2. Check config.json exists and is valid JSON
3. Check dependencies installed: `pip3 list`
4. Check error messages in console output

### Blockchain Monitor Not Connecting
1. Verify `POLYGON_RPC_WSS` is set in config.json
2. Test WebSocket URL manually: `python3 -c "from web3 import Web3; w3 = Web3(Web3.WebsocketProvider('YOUR_WSS_URL')); print(w3.is_connected())"`
3. Check Alchemy/Infura dashboard for API limits
4. Try setting `USE_BLOCKCHAIN_MONITOR=False` to use polling mode

### State File Corruption
1. Check for .bak1, .bak2, etc. backup files
2. Bot will automatically restore from last good backup
3. If all backups corrupted, delete state files and restart (will lose position history)

### Parity Match Rate Low (<90%)
1. Check blockchain monitor is actually receiving events (console logs)
2. Verify whale addresses are normalized (all lowercase)
3. Check system clock is accurate (time sync)
4. Increase `MIN_BLOCKCHAIN_CONFIRMATIONS` to 1 or 2 (trades reorg protection)

### High Memory Usage
1. Check metrics files size: `du -sh data/metrics/`
2. Rotate old metrics manually if needed
3. Restart bot weekly to clear in-memory caches
4. Consider reducing `STATE_BACKUP_GENERATIONS` to 3

---

## Success Criteria

✅ **Ready for Shadow Mode** when:
1. All validation tests pass
2. Bot runs 24+ hours without crashes
3. Parity match rate >90%
4. Health monitor shows HEALTHY
5. No state corruption incidents

✅ **Ready for LIVE** when:
1. 2 weeks clean shadow mode
2. Parity match rate >95%, side error <1%
3. Paper PnL positive in realistic/pessimistic scenarios
4. All edge cases tested (restart, network drop, market expiry)
5. User comfortable with risk management limits

---

## Emergency Rollback

If v14 has critical issues, rollback to v13:
```bash
git checkout v13
rm -rf data/metrics data/parity_reports
rm -f data/*.bak*
python3 run.py
```

Or disable new features:
```json
{
  "METRICS_LOGGING_ENABLED": false,
  "PARITY_CHECK_ENABLED": false,
  "HEALTH_MONITOR_ENABLED": false,
  "USE_BLOCKCHAIN_MONITOR": false
}
```
