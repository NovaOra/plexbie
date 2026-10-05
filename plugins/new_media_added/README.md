# New Media Added Plugin

Automatically posts rich notifications to Discord when new media is added to Plex via webhooks.

## Features

- **Rich Embeds**: Fetches poster images and descriptions from TMDB/TVDB
- **Smart Episode Batching**: Groups episodes from the same season added within 30 seconds
- **Message Editing**: Updates existing messages instead of spamming when multiple episodes are added
- **Monitored Shows Support**: Integrates with media_requests to handle ongoing shows
- **Multiple Media Types**: Supports movies, TV shows, and episodes

## Setup

### 1. Configure Plex Webhook

In your Plex server settings:

1. Go to Settings > Webhooks
2. Add a new webhook with URL: `http://YOUR_SERVER:8081/webhook/plex`
   - Replace `YOUR_SERVER` with your bot's IP/hostname
   - Port 8081 is the default webhook port (set in .env as WEBHOOK_PORT)

### 2. Environment Variables

Ensure these are set in your `.env`:

```env
# Required
TMDB_API_KEY=your_tmdb_api_key
STATS_CHANNEL_ID=your_discord_channel_id  # Channel for new media notifications
WEBHOOK_PORT=8081

# Optional but recommended
PLEX_URL=http://your-plex-server:32400
PLEX_TOKEN=your_plex_token
```

### 3. Create Updates Channel (if not exists)

The plugin posts to the channel specified in `STATS_CHANNEL_ID`. Make sure this channel exists and the bot has permission to:
- Send Messages
- Embed Links
- Read Message History (for editing existing messages)

## How It Works

### Episode Batching

When multiple episodes are added quickly (within 30 seconds):

1. **First episode**: Creates a new message
   ```
   📺 A new episode of Critical Role has been acquired!
   Critical Role - S04E01: Episode Title
   [Description and poster image]
   ```

2. **Subsequent episodes** (same season): Edits the message
   ```
   📺 A new episode of Critical Role has been acquired!
   Critical Role - Season 4, Episodes 1-3
   [Description and poster image]
   ```

3. **After 5 minutes or different season**: Creates a new message

### Monitored Shows

For shows with monitoring enabled (from media_requests):

- Tracks expected episode count for initial batch
- Once all initial episodes are posted, subsequent weekly episodes get their own messages
- Prevents spam when adding a complete season

### Movies & Full Shows

Movies and full TV shows get individual rich embeds with:
- Title and description from TMDB
- Poster image
- Links to TheTVDB (for TV) and Plex Web

## Files

- `cog.py` - Main plugin logic
- `plugin.json` - Plugin metadata
- `config/new_media_tracking.json` - Tracking data for episode batches (auto-created)

## Troubleshooting

### No notifications appearing

1. Check webhook is configured correctly in Plex
2. Verify `STATS_CHANNEL_ID` is correct
3. Check bot has permissions in that channel
4. Look at logs for webhook errors: `grep "Plex webhook" logs/plexbie.log`

### Duplicate messages

- Ensure only one webhook URL is configured in Plex
- Check that batch cleanup is running (every 5 minutes)

### Missing images

- Verify `TMDB_API_KEY` is set and valid
- Check that media in Plex has TMDB metadata agent enabled

## Integration with media_requests

The plugin checks if a show/season is being monitored by looking at media_requests data.
To enable full integration:

1. Ensure media_requests plugin is enabled
2. When approving requests with monitoring, the data is automatically tracked
3. The new_media_added plugin will adjust its behavior for monitored shows
