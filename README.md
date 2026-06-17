# UpdatedSite

Personal site for Rishi Singhvi with a live Spotify module.

## Run locally

```bash
python3 server.py
```

Then open [http://127.0.0.1:4173](http://127.0.0.1:4173).

## Spotify setup

1. Create a Spotify app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Add this redirect URI to the app settings:
   `http://127.0.0.1:4173/spotify/callback`
3. Copy `.env.example` to `.env.local`.
4. Fill in:
   `SPOTIFY_CLIENT_ID`
   `SPOTIFY_CLIENT_SECRET`
5. Start the local server with `python3 server.py`.
6. Open `http://127.0.0.1:4173/spotify/login` once from your machine to authorize your Spotify account.
7. The callback stores `SPOTIFY_REFRESH_TOKEN` in `.env.local`.
8. Refresh the site and the Spotify module will switch from setup mode to live data mode.

## Notes

- The local auth route is intentionally loopback-only so public visitors can not overwrite the Spotify account tied to the site.
- The Spotify module uses your current track plus your recently played tracks.
- Spotify's docs say development mode apps are limited and note that the app owner should have Spotify Premium for development-mode apps to function cleanly.
