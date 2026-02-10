import datetime

def log_decision(decision, reason):
    timestamp = datetime.datetime.now().isoformat()
    entry = f"{timestamp} | {decision} | {reason}\n"
    with open("audit_log.txt", "a") as f:
        f.write(entry)
