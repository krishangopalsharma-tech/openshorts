# Background music library

Tracks in this folder show up in the dashboard's music picker (`GET /api/music`)
and can be mixed under a clip's voice from the clip card. Drop your own
`.mp3/.m4a/.wav/.aac/.ogg/.flac` here or upload through the UI
(`POST /api/music/upload`); uploads land in this same folder.

The five bundled tracks were taken over from ClipForge's library. Their
filenames follow Pixabay's `artist-title-id` pattern, i.e. Pixabay Content
License (free for commercial use, no attribution required). Verify before
shipping them in a paid product; replace them if in doubt.
