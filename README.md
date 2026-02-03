# Telegram Auto Listener & Smart Sender

A scalable Telegram automation system that listens to group messages, detects keywords,
stores structured data, and safely sends messages using multiple sender accounts.

## Features
- Listen-only Telegram account (no sending)
- Keyword-based message detection with exact-word logic
- Multi-session sender accounts with cooldown protection
- Permanent & temporary failure handling
- SQLite-backed message storage and tagging
- Moderator UI bot with inline buttons and filters
- CSV logging and export (messages, stats, history)
- Privacy-aware handling for users without usernames

## Tech Stack
- Python 3
- Pyrogram
- AsyncIO
- SQLite
- YAML Configuration
- Telegram Bot API

## Architecture
- Listener Account → Captures messages and users
- Database Layer → Stores messages, tags, and status
- Sender Accounts → Deliver messages safely with cooldown logic
- Moderator Bot → Review, tag, filter, export, and manage data

## Use Cases
- Educational platforms
- Tutoring request detection
- Lead capture from Telegram groups
- Multi-group moderation and analytics

## Status
Production-ready / Actively used
