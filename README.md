# Spoto DJ

Personal DJ analysis tool for Spotify liked songs.  
Fetches your entire Spotify library and enriches it with BPM, Camelot key, and musical key data.

## Features

- Connect your Spotify account via OAuth
- Browse all liked songs with DJ-ready metadata
- Filter by BPM range, Camelot key, energy
- Export to CSV

## Data Sources

BPM and key data provided by [GetSongBPM](https://getsongbpm.com).

## Stack

- FastAPI (Python) — backend + Spotify OAuth
- Vanilla JS — frontend
- [GetSongBPM API](https://getsongbpm.com/api) — BPM & key lookup

## Setup

```bash
cp .env.example .env   # fill in Spotify + GetSongBPM credentials
pip install -r requirements.txt
uvicorn main:app --reload
```
