import time
import os
import subprocess
import sys
import logging
import json
import smtplib
import re
from email.mime.text import MIMEText
from email.header import Header
from utils.lock import ScannerLock, LockError
from utils.logger import setup_logging

# Configure logging to write to both stdout and logs/scanner.log with a line limit of 100,000 lines
logger = setup_logging('logs/scanner.log')

def run_script(script_name, *args):
    logger.info("=========================================")
    logger.info(f"Starting {script_name} at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=========================================")
    summary_lines = []
    process = None
    try:
        cmd = [sys.executable, "-u", script_name] + list(args)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=env
        )

        import threading
        last_output_ts = [time.monotonic()]
        watchdog_stop = threading.Event()

        def _stall_watchdog():
            while not watchdog_stop.wait(30.0):
                if process.poll() is not None:
                    break
                idle_sec = time.monotonic() - last_output_ts[0]
                if idle_sec > 900.0:  # 15 minutes with zero log output -> hung child
                    logger.warning(f"[WATCHDOG] {script_name} produced no output for {int(idle_sec)}s; terminating hung process...")
                    try:
                        process.terminate()
                        time.sleep(5)
                        if process.poll() is None:
                            process.kill()
                    except Exception:
                        pass
                    break

        wd_thread = threading.Thread(target=_stall_watchdog, daemon=True)
        wd_thread.start()

        try:
            if process.stdout:
                for line in process.stdout:
                    last_output_ts[0] = time.monotonic()
                    line_str = line.rstrip('\r\n')
                    logger.info(line_str)
                    # Capture summary and finished markers
                    if any(x in line_str for x in ["Scan Finished", "Sync Finished", "GLOBAL CALIBRE SYNC SUMMARY", "Discovered:", "Synced:", "Failed:", "Skipped:", "[SUMMARY]", "Files to process:", "MyLibrary BookDB:", "[LIMIT REACHED]"]):
                        summary_lines.append(line_str)
        finally:
            watchdog_stop.set()
                    
        return_code = process.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, cmd)
            
        logger.info(f"Finished {script_name} successfully.")
    except subprocess.CalledProcessError as e:
        err_msg = f"[ERROR] Process exited with non-zero code {e.returncode}"
        logger.error(f"Error running {script_name}: {e}")
        summary_lines.append(err_msg)
    except Exception as e:
        err_msg = f"[ERROR] Unexpected runtime error: {str(e)}"
        logger.error(f"Unexpected error running {script_name}: {e}")
        summary_lines.append(err_msg)
    finally:
        if process is not None and process.poll() is None:
            logger.warning(f"Terminating lingering child process for {script_name} (PID {process.pid})...")
            try:
                process.terminate()
                process.wait(timeout=10)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass
    return summary_lines

def send_status_email(summary_reports):
    config_path = "config.json"
    if not os.path.exists(config_path):
        return
        
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
            
        email_config = config.get("email")
        if not email_config or not email_config.get("enabled", False):
            return
            
        smtp_server = email_config.get("smtp_server", "smtp.gmail.com")
        smtp_port = int(email_config.get("smtp_port", 587))
        smtp_user = email_config.get("smtp_user")
        smtp_password = email_config.get("smtp_password")
        to_email = email_config.get("to_email")
        
        if not smtp_user or not smtp_password or not to_email:
            logger.warning("Email configurations are incomplete. Skipping email notification.")
            return
            
        # Analyze summary reports for any error signals
        has_errors = False
        for lines in summary_reports.values():
            for line in lines:
                # Check for explicit [ERROR] prefix
                if "[ERROR]" in line:
                    has_errors = True
                    break
                # Check if "Failed: <count>" has a value > 0
                failed_match = re.search(r'Failed:\s*([1-9]\d*)', line)
                if failed_match:
                    has_errors = True
                    break
            if has_errors:
                break
                
        status_label = "[ALERT: ERROR]" if has_errors else "[SUCCESS]"
        subject = f"{status_label} MyLibrary Sync Status Report - {time.strftime('%Y-%m-%d %H:%M:%S')}"
        
        body_lines = [
            "MyLibrary Sync Status Report",
            f"Overall Status: {'ERRORS/FAILURES DETECTED' if has_errors else 'SUCCESS'}",
            f"Execution Time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "=========================================",
            ""
        ]
        
        for script_name, lines in summary_reports.items():
            body_lines.append(f"--- {script_name} Results ---")
            if lines:
                body_lines.extend(lines)
            else:
                body_lines.append("No summary lines found (script failed or returned no stats).")
            body_lines.append("")
            
        body_lines.append("=========================================")
        body_lines.append("Check logs/scanner.log on your NAS for the complete logs.")
        body = "\n".join(body_lines)
        
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['From'] = Header(smtp_user, 'utf-8')
        msg['To'] = Header(to_email, 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        
        logger.info(f"Connecting to SMTP server {smtp_server}:{smtp_port} to send notification...")
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [to_email], msg.as_string())
        server.quit()
        logger.info("Sync status report email sent successfully.")
    except Exception as e:
        err_str = str(e)
        if "534" in err_str or "Application-specific password required" in err_str:
            logger.error("Failed to send sync status email: Gmail requires a 16-character App Password (not standard account password). Please generate one in Google Account -> Security -> 2-Step Verification -> App Passwords and configure it in config.json under email.smtp_password.")
        else:
            logger.error(f"Failed to send sync status email: {e}")


def main():
    interval_hours = float(os.environ.get('SCAN_INTERVAL_HOURS', '4'))
    if os.path.exists("config.json"):
        try:
            with open("config.json", 'r') as f:
                c_data = json.load(f)
                if c_data.get("scan_interval_hours"):
                    interval_hours = float(c_data["scan_interval_hours"])
        except Exception:
            pass
    interval_seconds = interval_hours * 3600
    
    lock_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nas_scanner_loop.lock')
    try:
        with ScannerLock(lock_file):
            # Check for config path inside container
            config_in_container = "/config/config.json"
            if os.path.exists(config_in_container) and not os.path.exists("config.json"):
                try:
                    os.symlink(config_in_container, "config.json")
                    logger.info(f"Linked config file from {config_in_container}")
                except Exception as e:
                    import shutil
                    shutil.copy(config_in_container, "config.json")
                    logger.info(f"Copied config file from {config_in_container}: {e}")

            logger.info(f"Starting MyLibrary Scanner Loop (Interval: {interval_hours} hours)...")
            
            trigger_file = os.path.join("logs", "trigger_scan.flag")
            while True:
                if os.path.exists(trigger_file):
                    try:
                        os.remove(trigger_file)
                    except Exception:
                        pass

                summary_reports = {}

                # Run unified book/magazine scanner + epubmeta enricher (single os.walk pass)
                if os.path.exists("sync_files.py"):
                    summary_reports["sync_files.py (Unified Sync & Enrichment)"] = run_script("sync_files.py")

                
                # Run audiobook scanner
                if os.path.exists("sync_audiobooks.py"):
                    summary_reports["sync_audiobooks.py"] = run_script("sync_audiobooks.py")
                
                # Run calibre scanner if libraries are configured
                has_calibre_config = False
                if os.path.exists("config.json"):
                    try:
                        with open("config.json", 'r') as f:
                            c_data = json.load(f)
                            if c_data.get("calibre_libraries"):
                                has_calibre_config = True
                            if c_data.get("scan_interval_hours"):
                                interval_hours = float(c_data["scan_interval_hours"])
                                interval_seconds = interval_hours * 3600
                    except Exception:
                        pass

                if has_calibre_config and os.path.exists("sync_calibre.py"):
                    summary_reports["sync_calibre.py"] = run_script("sync_calibre.py")

                    
                # Send email report
                send_status_email(summary_reports)
                    
                logger.info(f"\nAll scans finished. Next scan in {interval_hours} hours. Sleeping...")
                slept = 0.0
                while slept < interval_seconds:
                    if os.path.exists(trigger_file):
                        try:
                            os.remove(trigger_file)
                        except Exception:
                            pass
                        logger.info("Manual scan trigger detected! Starting new scan cycle immediately...")
                        break
                    time.sleep(2.0)
                    slept += 2.0
    except LockError as e:
        logger.error(f"Error starting scanner loop: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
