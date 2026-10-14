# Private Discord Music Bot — Railway Edition

A purpose-built music bot with only two text commands and one polished Discord control panel. It is intended for one private Discord server and one voice session per server.

## What you get

- **`.join`** — join the voice channel you are in, or move the bot to another channel. It stays connected while idle, even when no humans are in the channel.
- **`.play <link>`** — play a supported public link; tracks and playlist items are queued.
- **Control panel buttons** — Pause/Resume, Next, Stop, and Repeat Current Track.
- **No extra text commands** — only `.join` and `.play` are registered.
- **Restart recovery** — a Railway Volume stores the last voice channel, panel message, current URL, queue URLs, and repeat setting. The bot tries to rejoin and replay after deployment/restart.
- **Private audio engine** — Lavalink has no public domain or TCP proxy. The bot reaches it over Railway private networking.
- **Audio tuning** — maximum Opus encoder quality (`10`) and high resampling quality are configured. The source's original quality and the Discord voice channel bitrate still set practical limits.

## Read these limitations first

1. **One bot account can be in only one voice channel in a given Discord server at a time.** Use `.join` from a different voice channel to move it there. Simultaneous playback in multiple voice channels requires separate bot accounts and bot instances.
2. “Any link” can only mean *best-effort support for public, compatible sources*. YouTube/YouTube Music, SoundCloud, Bandcamp, Twitch, Vimeo, direct media URLs, and many other public URLs that `yt-dlp` supports are attempted. Spotify/Apple Music are metadata-to-audio matching integrations, not direct playback of their DRM-protected streams. Private, login-only, expired, region-blocked, DRM-protected or source-broken links may fail. No bot can guarantee every link will work forever.
3. **24/7 means the bot is configured not to idle-disconnect and to recover after restarts.** It cannot prevent Railway, Discord, or a media provider from having an outage. After a restart it resumes the saved link from the beginning, not at the exact prior timestamp.
4. Railway billing is usage-based on top of the plan's monthly minimum. This project runs **two always-on services**. Check Workspace → Usage/Billing after deployment and set spending controls. Railway's Hobby plan is currently $5/month and includes $5 of usage; Pro is $20/month and includes $20 of usage. You do not need Pro solely because a bot should stay online, but actual usage can exceed either included amount. See <https://railway.com/pricing>.

---

# Step 1 — Create the Discord bot

1. Open <https://discord.com/developers/applications> and choose **New Application**.
2. Give the application a name. Open **Bot** and add/create the bot user if the portal asks.
3. Copy the bot token and keep it private. Do **not** paste it into this README, source code, or GitHub. If you expose it, reset the token immediately.
4. On the **Bot** page, enable **Message Content Intent**. The bot uses `.join` and `.play` message commands, so it needs this intent.
5. For maximum privacy, turn **Public Bot** off if the option is available. The bot also checks the configured server ID in code.
6. Open **OAuth2 → URL Generator**. Select the `bot` scope. Under Bot Permissions, select:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
   - Connect
   - Speak
7. Open the generated invite URL, choose your private server, and authorize it.

## Step 2 — Copy the private server ID

1. Discord → **User Settings → Advanced** → turn on **Developer Mode**.
2. Right-click your server icon/name and choose **Copy Server ID**.
3. Keep this number for the `GUILD_ID` variable in Railway. It locks commands and recovery to that server.

Make sure the bot's role/channel overrides allow it to view and send messages in the text channel, embed links, and connect/speak in your voice channel.

---

# Step 3 — Put the project on a private GitHub repository

Railway will build two separate services from the same repository. One is the Python Discord bot; the other is Lavalink, the audio engine.

### Simple method: GitHub Desktop

1. Install GitHub Desktop: <https://desktop.github.com/> and sign in.
2. On GitHub.com, create a **private, empty repository** named `private-discord-music-bot`. Do not initialize it with another README.
3. In GitHub Desktop, clone that repository to your computer.
4. Extract this ZIP. Open its `railway-private-music-bot` folder and copy **everything inside it** into the cloned repository folder. The repository root must directly contain `README.md`, `.gitignore`, `bot/`, and `lavalink/`.
5. In GitHub Desktop, commit the changes and click **Push origin**.
6. Check GitHub.com: both `bot/` and `lavalink/` should be visible at the repository root. Do not add real tokens or passwords.

---

# Step 4 — Create your Railway project

1. Open <https://railway.com/> and sign in.
2. Create **New Project → Empty Project**. Name it `private-music-bot`.
3. Create both services in the **same project and same environment** so they can use private networking.

## Step 5 — Deploy the `lavalink` service first

1. Choose **New → GitHub Repo** and select your private repository.
2. Rename the service exactly **`lavalink`**.
3. In the service's **Settings → Build**, set **Root Directory** to `/lavalink`. Railway should find the Dockerfile in that directory.
4. Do **not** add a public domain or public TCP proxy. This service is internal only.
5. In **Variables**, add:

   ```env
   LAVALINK_PASSWORD=PASTE_A_LONG_RANDOM_PASSWORD_HERE
   JAVA_TOOL_OPTIONS=-Xms128m -Xmx768m
   SPOTIFY_CLIENT_ID=
   SPOTIFY_CLIENT_SECRET=
   APPLE_MUSIC_API_TOKEN=
   ```

   Generate a long random password (32+ characters). You will paste the exact same password into the bot service. Keep the optional music-service variables blank for the initial test if you prefer; add credentials later to improve metadata-based link matching.
6. Deploy the service. In **Deployments → Logs**, wait for Lavalink to finish loading the YouTube and LavaSrc plugins and report that the server is listening on port `2333`.
7. Keep its name exactly `lavalink`; the bot uses the private hostname `lavalink.railway.internal`.

If the build fails downloading `yt-dlp` or a Lavalink plugin, use the full build log to identify the failed URL/version. Do not make Lavalink public to “fix” a connection issue.

## Step 6 — Deploy the `music-bot` service

1. In the same Railway project, choose **New → GitHub Repo** and select the same private repository again.
2. Rename the service exactly **`music-bot`**.
3. In **Settings → Build**, set **Root Directory** to `/bot` so Railway uses `bot/Dockerfile`.
4. In **Variables**, open Raw Editor and add these lines. Replace the two placeholders with your real values and the shared Lavalink password:

   ```env
   DISCORD_TOKEN=YOUR_REAL_DISCORD_BOT_TOKEN
   GUILD_ID=YOUR_PRIVATE_DISCORD_SERVER_ID
   LAVALINK_HOST=lavalink.railway.internal
   LAVALINK_PORT=2333
   LAVALINK_PASSWORD=THE_EXACT_SAME_RANDOM_PASSWORD_AS_LAVALINK
   STATE_FILE=/data/state.json
   LOG_LEVEL=INFO
   ```

   Do not include the literal placeholder text. Never put your token in GitHub.
5. Add persistent storage: open **Settings → Volumes → Add Volume**, select this bot service, and set the mount path to exactly `/data`. The bot runs as the default root user in its Python container, so it can write state there.
6. Open the service's deployment/restart settings. Keep it running continuously; **do not enable Serverless/sleeping** for this voice bot. Set the restart policy to **Always** (or the closest option available) if the UI offers it.
7. Deploy. In the bot logs, look for messages similar to `Discord connected as ...` and `Lavalink is ready`. The bot dependency list includes `discord.py[voice]`, so the required voice encryption package is installed during the build.

Do not generate a public domain for either service. The bot talks to Discord through its outgoing connection; Lavalink is reached using Railway's private network. Railway private DNS uses `<service-name>.railway.internal` and services must be in the same environment: <https://docs.railway.com/networking/private-networking>.

## Step 7 — Test the bot

1. Join the voice channel where you want to listen.
2. In the text channel where the control panel should appear, type:

   ```text
   .join
   ```

3. Then paste a public link:

   ```text
   .play https://www.youtube.com/watch?v=VIDEO_ID
   ```

   Replace the example with a real public video URL.
4. Check that the embed/control panel appears and that the audio starts. Test each button:
   - **Pause/Resume** pauses and resumes the current track.
   - **Next** skips the current track and starts the next queued item; if the queue is empty, playback stops but voice stays connected.
   - **Stop** clears current playback and the queue but keeps the bot connected to voice.
   - **Loop** repeats the current track until turned off.
5. Join a different voice channel and type `.join` there. The bot should move to that channel. Only one voice channel per server can be active per bot account.
6. Restart/redeploy `music-bot`. With the `/data` volume attached, it should try to rejoin the saved channel and restore the saved current URL and queue. The current link starts from the beginning.

## Step 8 — Optional: improve Spotify / Apple Music resolution

This project configures LavaSrc to resolve Spotify and Apple Music track metadata and try to match playable audio using YouTube search. It does **not** decrypt subscription audio.

For Spotify:
1. Open <https://developer.spotify.com/dashboard> and create an app.
2. Copy its Client ID and Client Secret into the **`lavalink` service Variables** as `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`.
3. Deploy `lavalink` again.

For Apple Music, add a valid MusicKit/Apple Music API token as `APPLE_MUSIC_API_TOKEN` on **`lavalink`**, then redeploy it. This is optional. Do not place any of these credentials in the bot service or GitHub.

## Everyday use

- `.join` — join or move to the voice channel you are currently in; remains connected when idle.
- `.play <link>` — play a public supported media URL or queue playlist tracks.
- The panel is the only playback UI: Pause/Resume, Next, Stop, Repeat Current Track.

Commands/buttons are restricted to the configured server. The controls only work for someone currently in the same voice channel as the bot. The bot intentionally has no `.leave`, `.skip`, `.stop`, `.loop`, `.help`, dashboard, or other text commands.

---

# Troubleshooting

### Bot is online but ignores `.join` and `.play`
- Verify `Message Content Intent` is enabled on the bot in the Discord Developer Portal.
- Verify `DISCORD_TOKEN` and `GUILD_ID`, then redeploy `music-bot`.
- Make sure the command is being sent in the configured private server and starts with a dot (`.join` or `.play`).

### Logs say Lavalink is starting/not ready
- Confirm the services are named exactly `lavalink` and `music-bot`.
- Confirm both are in the same Railway project **and environment**.
- Match `LAVALINK_PASSWORD` exactly between services.
- Ensure the Lavalink deployment completed and its logs show port `2333` listening.

### Bot joins but no audio
- Confirm Connect/Speak permission in the selected voice channel.
- Try a regular public YouTube link first, to establish the baseline.
- Try a SoundCloud URL as a second source test.
- A private, deleted, login-required, DRM-protected, geo-blocked or expired link may not play. Sources can change their extraction APIs at any time.
- For Spotify/Apple URLs, check the LavaSrc startup log and add the matching optional credentials described above.

### Buttons appear but do not work
- The person clicking must be in the same voice channel as the bot.
- Give the bot View Channel, Send Messages, Embed Links, and Read Message History in the control-panel text channel.
- Check `music-bot` logs for permission failures.

### The bot leaves or fails to return after a deploy
- Check that the bot service has a Railway Volume mounted at `/data` and `STATE_FILE=/data/state.json`.
- Keep serverless/sleep mode disabled and the restart policy enabled.
- The saved voice channel must still exist and the bot must have Connect/Speak permission there.
- No paid plan can prevent a Discord/Railway/source outage, and reconnects after a platform disruption are best-effort.

### Watch your bill
The Hobby plan currently includes $5 of usage after its $5 minimum; Pro includes $20 of usage after its $20 minimum. The bot and Lavalink are both always running, so check Railway **Workspace → Usage/Billing** after the first several days and set usage alerts/limits before leaving it unattended. Current details: <https://railway.com/pricing>.

## Security checklist

- Keep the GitHub repository private.
- Keep `DISCORD_TOKEN`, Lavalink password, Spotify secrets and Apple tokens in Railway Variables only.
- Never generate public networking for the Lavalink service.
- Turn off **Public Bot** in Discord Developer Portal if available.
- If you leak the Discord token, reset it in Discord Developer Portal and update Railway Variables.
