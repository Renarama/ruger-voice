# rutger-voice

Bro mellan 46elks websocket-samtal och ElevenLabs-agenten Rutger.

## Deploy på Fly.io
1. Lägg filerna i ett nytt GitHub-repo `rutger-voice`.
2. Fly.io → Launch an App → välj repot. Appnamn: `rutger-voice`, region Stockholm (arn).
3. Secrets i Fly-appen:
   - `ELEVENLABS_API_KEY`
   - `RUTGER_AGENT_ID`
   - `SUPABASE_URL` = https://nrlnbzjmmvlltcfyihgf.supabase.co
   - `SUPABASE_SERVICE_ROLE_KEY`
4. Kontroll: https://rutger-voice.fly.dev ska visa "Rutger OK".

## 46elks
Websocket-numrets `websocket_url` = `wss://rutger-voice.fly.dev`

## ElevenLabs
Agentens ljudformat (in och ut): PCM 16000 Hz.
