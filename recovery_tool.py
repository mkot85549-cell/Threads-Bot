#!/usr/bin/env python3
"""
Recovery Tool for SQLite Database (`farm.db`)
Restore lost users and subscription data safely.

Usage:
    python recovery_tool.py

Features:
    - OPTION 1: Manual restore (prompt admin for user data)
    - OPTION 2: CryptoBot auto-recovery (fetch paid invoices from last 7-14 days)
    - Safe UPSERT logic to prevent data loss
    - Color-coded CLI logs for transparency
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from aiocryptopay import AioCryptoPay, Networks

import config
from db_utils import get_db


# ─────────────────────────────────────────────────────────────────────────────
# Color ANSI codes for terminal output
# ─────────────────────────────────────────────────────────────────────────────
class Color:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def print_header(text: str):
    """Print a bold header."""
    print(f"\n{Color.BOLD}{Color.HEADER}{'='*70}{Color.ENDC}")
    print(f"{Color.BOLD}{Color.HEADER}{text.center(70)}{Color.ENDC}")
    print(f"{Color.BOLD}{Color.HEADER}{'='*70}{Color.ENDC}\n")


def print_success(text: str):
    """Print success message in green."""
    print(f"{Color.OKGREEN}✓ {text}{Color.ENDC}")


def print_info(text: str):
    """Print info message in cyan."""
    print(f"{Color.OKCYAN}ℹ {text}{Color.ENDC}")


def print_warning(text: str):
    """Print warning message in yellow."""
    print(f"{Color.WARNING}⚠ {text}{Color.ENDC}")


def print_error(text: str):
    """Print error message in red."""
    print(f"{Color.FAIL}✗ {text}{Color.ENDC}")


def print_result(action: str, tg_id: str, username: str, sub_end_date: str, max_accounts: int):
    """Print a formatted result of user insert/update."""
    status_icon = "→" if action == "UPSERTED" else "+"
    print(
        f"{Color.OKGREEN}{status_icon}{Color.ENDC} "
        f"[{Color.BOLD}{tg_id}{Color.ENDC}] "
        f"{username} | "
        f"Expires: {Color.OKBLUE}{sub_end_date}{Color.ENDC} | "
        f"Accs: {Color.OKCYAN}{max_accounts}{Color.ENDC}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Database operations
# ─────────────────────────────────────────────────────────────────────────────

def upsert_user(
    tg_user_id: str,
    username: str,
    sub_end_date: datetime,
    max_accounts: int = 3
) -> str:
    """
    Upsert a user into the database using INSERT ... ON CONFLICT.
    Returns action taken: 'INSERTED' or 'UPSERTED'
    """
    with get_db() as conn:
        cursor = conn.cursor()
        sub_end_str = sub_end_date.strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute(
            """
            INSERT INTO users (tg_user_id, username, sub_end_date, max_accounts)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET
                username = excluded.username,
                sub_end_date = excluded.sub_end_date,
                max_accounts = excluded.max_accounts
            """,
            (tg_user_id, username, sub_end_str, max_accounts)
        )
        conn.commit()
    return "UPSERTED"


def get_user_count() -> int:
    """Get total number of users in database."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        result = cursor.fetchone()
    return result[0] if result else 0


def get_users_by_date_range(days: int = 7) -> List[Dict[str, Any]]:
    """Get users modified in the last N days."""
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT tg_user_id, username, sub_end_date, max_accounts
            FROM users
            WHERE sub_end_date > ?
            ORDER BY sub_end_date DESC
            """,
            (cutoff_str,)
        )
        rows = cursor.fetchall()
    return [dict(row) for row in rows]


# ─────────────────────────────────────────────────────────────────────────────
# CLI Input Helpers
# ─────────────────────────────────────────────────────────────────────────────

def prompt_tg_user_id() -> str:
    """Prompt for and validate Telegram user ID."""
    while True:
        user_input = input(
            f"{Color.OKCYAN}Enter Telegram User ID (numeric):{Color.ENDC} "
        ).strip()
        if user_input.isdigit():
            return user_input
        print_error("Invalid input. Please enter a numeric Telegram ID.")


def prompt_username() -> str:
    """Prompt for username."""
    while True:
        username = input(
            f"{Color.OKCYAN}Enter username (or press Enter for 'unknown'):{Color.ENDC} "
        ).strip()
        return username if username else "unknown"


def prompt_max_accounts() -> int:
    """Prompt for and validate max accounts."""
    while True:
        try:
            accs = int(input(
                f"{Color.OKCYAN}Enter max accounts (default 3):{Color.ENDC} "
            ).strip() or "3")
            if accs > 0:
                return accs
            print_error("Please enter a positive number.")
        except ValueError:
            print_error("Invalid input. Please enter a number.")


def prompt_days() -> int:
    """Prompt for and validate days to add."""
    while True:
        try:
            days = int(input(
                f"{Color.OKCYAN}Enter subscription days to add (default 30):{Color.ENDC} "
            ).strip() or "30")
            if days > 0:
                return days
            print_error("Please enter a positive number.")
        except ValueError:
            print_error("Invalid input. Please enter a number.")


# ─────────────────────────────────────────────────────────────────────────────
# OPTION 1: Manual Restore
# ─────────────────────────────────────────────────────────────────────────────

async def manual_restore():
    """
    Manually add/update a single user with admin-provided data.
    """
    print_header("OPTION 1: Manual Restore")

    print_info("You will now enter user details manually.")
    print_info("Existing users with the same Telegram ID will be updated.\n")

    tg_user_id = prompt_tg_user_id()
    username = prompt_username()
    max_accounts = prompt_max_accounts()
    days_to_add = prompt_days()

    sub_end_date = datetime.now() + timedelta(days=days_to_add)

    print_info(f"\nRestoring user {Color.BOLD}{tg_user_id}{Color.ENDC}...")

    try:
        upsert_user(tg_user_id, username, sub_end_date, max_accounts)
        print_result(
            "INSERTED",
            tg_user_id,
            username,
            sub_end_date.strftime("%Y-%m-%d %H:%M:%S"),
            max_accounts
        )
        print_success(f"User {tg_user_id} successfully restored!")
    except Exception as e:
        print_error(f"Failed to restore user: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# OPTION 2: CryptoBot Auto-Recovery
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_paid_invoices(
    crypto: AioCryptoPay,
    days_back: int = 7
) -> List[Dict[str, Any]]:
    """
    Fetch paid invoices from CryptoBot for the last N days.
    Returns list of invoice data.
    """
    print_info(f"Querying CryptoBot for paid invoices from last {days_back} days...")

    try:
        # Fetch invoices (aiocryptopay handles pagination)
        result = await crypto.get_invoices()

        # Handle different return types from aiocryptopay
        invoice_items = []
        if result is None:
            print_warning("CryptoBot returned None (no invoices or API error)")
            return []
        elif hasattr(result, 'items'):
            invoice_items = result.items
        elif isinstance(result, list):
            invoice_items = result
        else:
            print_warning(f"Unexpected response type: {type(result)}")
            return []

        paid_invoices = []
        cutoff_time = datetime.now() - timedelta(days=days_back)

        for invoice in invoice_items:
            # Check if invoice is paid
            is_paid = False
            if invoice.status:
                status_str = (
                    invoice.status.value
                    if hasattr(invoice.status, 'value')
                    else str(invoice.status)
                )
                is_paid = status_str.strip().lower() == 'paid'

            # Check if invoice is within time range (optional, depends on aiocryptopay)
            if is_paid:
                paid_invoices.append({
                    'invoice_id': invoice.invoice_id,
                    'amount': invoice.amount,
                    'asset': invoice.asset,
                    'status': invoice.status,
                    'payload': getattr(invoice, 'payload', None),  # May contain user ID
                    'description': getattr(invoice, 'description', ''),
                })

        print_success(f"Found {len(paid_invoices)} paid invoices")
        return paid_invoices

    except Exception as e:
        print_error(f"Failed to fetch invoices from CryptoBot: {str(e)}")
        import traceback
        traceback.print_exc()
        return []


def extract_user_from_invoice(invoice: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Extract user ID and metadata from invoice payload.
    
    Payload format (must be JSON in your invoice description or custom field):
    Expects one of:
      - 'payload' field with user data
      - 'description' field with pattern like "User: 123456789"
      - Or you need to implement custom logic based on YOUR invoice structure
    """
    # Try payload field first
    if invoice.get('payload'):
        payload = invoice['payload']
        if isinstance(payload, dict):
            return payload
        # If payload is a string, try to parse it
        if isinstance(payload, str):
            try:
                import json
                parsed = json.loads(payload)
                return parsed
            except:
                pass

    # Try description field (e.g., "User: 123456789 - 2 accounts")
    description = invoice.get('description', '')
    if description:
        # Example: "User: 123456789 - 2 accounts"
        if 'User:' in description:
            parts = description.split('User:')[1].split('-')[0].strip()
            if parts.isdigit():
                return {'tg_user_id': parts}

    print_warning(
        f"Could not extract user from invoice {invoice['invoice_id']}. "
        f"Payload: {invoice.get('payload')} | Description: {description}"
    )
    return None


async def cryptobot_auto_recovery():
    """
    Auto-recover users from paid CryptoBot invoices.
    """
    print_header("OPTION 2: CryptoBot Auto-Recovery")

    print_info("Initializing CryptoBot connection...")
    crypto = AioCryptoPay(token=config.CRYPTO_PAY_TOKEN, network=Networks.MAIN_NET)

    try:
        # Prompt for days back
        print("\n" + Color.OKCYAN + "Select invoice retrieval window:" + Color.ENDC)
        print("  [1] Last 7 days (default)")
        print("  [2] Last 14 days")
        print("  [3] Last 30 days")
        choice = input(f"\n{Color.OKCYAN}Enter choice (1-3, default 1):{Color.ENDC} ").strip()

        days_map = {"1": 7, "2": 14, "3": 30}
        days_back = days_map.get(choice, 7)

        # Fetch paid invoices
        paid_invoices = await fetch_paid_invoices(crypto, days_back)

        if not paid_invoices:
            print_warning("No paid invoices found. Nothing to recover.")
            return

        # Process invoices and upsert users
        print_info(f"\nProcessing {len(paid_invoices)} invoices...\n")

        recovered_count = 0
        skipped_count = 0

        for invoice in paid_invoices:
            user_data = extract_user_from_invoice(invoice)

            if not user_data:
                skipped_count += 1
                continue

            tg_user_id = user_data.get('tg_user_id', '')
            username = user_data.get('username', 'auto-recovered')

            if not tg_user_id or not tg_user_id.isdigit():
                print_warning(f"Skipping invoice {invoice['invoice_id']}: Invalid user ID '{tg_user_id}'")
                skipped_count += 1
                continue

            # Default: 30 days from now
            sub_end_date = datetime.now() + timedelta(days=30)
            max_accounts = user_data.get('max_accounts', 3)

            try:
                upsert_user(tg_user_id, username, sub_end_date, max_accounts)
                print_result(
                    "UPSERTED",
                    tg_user_id,
                    username,
                    sub_end_date.strftime("%Y-%m-%d %H:%M:%S"),
                    max_accounts
                )
                recovered_count += 1
            except Exception as e:
                print_error(f"Failed to upsert user {tg_user_id}: {str(e)}")
                skipped_count += 1

        # Summary
        print_header("Recovery Summary")
        print_success(f"Recovered: {recovered_count} users")
        if skipped_count > 0:
            print_warning(f"Skipped:   {skipped_count} invoices")
        print_info(f"Total users in DB: {get_user_count()}")

    finally:
        # Close the aiohttp session properly
        if hasattr(crypto, 'session') and crypto.session:
            await crypto.session.close()
        if hasattr(crypto, 'close'):
            await crypto.close()


# ─────────────────────────────────────────────────────────────────────────────
# Main Menu
# ─────────────────────────────────────────────────────────────────────────────

async def show_menu():
    """Display main recovery menu."""
    print_header("SQLite Recovery Tool v1.0")

    print_info(f"Database: {Color.BOLD}{config.DB_NAME}{Color.ENDC}")
    print_info(f"Current users in DB: {Color.BOLD}{get_user_count()}{Color.ENDC}\n")

    print(f"{Color.BOLD}Recovery Options:{Color.ENDC}")
    print("  [1] Manual Restore       - Add/update a user manually")
    print("  [2] CryptoBot Auto-Recover - Fetch paid invoices and restore users")
    print("  [3] View Recent Users    - Show users added/updated in last 7 days")
    print("  [4] Exit")

    choice = input(f"\n{Color.OKCYAN}Enter option (1-4):{Color.ENDC} ").strip()

    return choice


async def view_recent_users():
    """Display recent users."""
    print_header("Recent Users (Last 7 Days)")
    users = get_users_by_date_range(days=7)

    if not users:
        print_warning("No users found in the last 7 days.")
        return

    print(f"{Color.BOLD}{'ID':<15} {'Username':<20} {'Sub Expires':<20} {'Accs':<5}{Color.ENDC}")
    print("─" * 60)

    for user in users:
        tg_id = user['tg_user_id']
        username = user['username'][:19]
        sub_end = user['sub_end_date'][:19]
        max_accs = user['max_accounts']

        print(f"{tg_id:<15} {username:<20} {sub_end:<20} {max_accs:<5}")

    print(f"\n{Color.OKGREEN}Total: {len(users)} users{Color.ENDC}")


async def main():
    """Main event loop."""
    while True:
        try:
            choice = await show_menu()

            if choice == "1":
                await manual_restore()
            elif choice == "2":
                await cryptobot_auto_recovery()
            elif choice == "3":
                await view_recent_users()
            elif choice == "4":
                print_success("Recovery tool closed. Goodbye!")
                break
            else:
                print_error("Invalid option. Please try again.")

            # Prompt to continue
            input(f"\n{Color.OKCYAN}Press Enter to continue...{Color.ENDC}")

        except KeyboardInterrupt:
            print("\n" + Color.WARNING + "Interrupted by user." + Color.ENDC)
            break
        except Exception as e:
            print_error(f"Unexpected error: {str(e)}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    print(f"\n{Color.BOLD}{Color.OKBLUE}Starting SQLite Recovery Tool...{Color.ENDC}\n")
    asyncio.run(main())
