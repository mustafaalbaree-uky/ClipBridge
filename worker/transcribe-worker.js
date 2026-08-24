// Optional: Cloudflare Worker that proxies audio to Groq Whisper.
// Only needed for the Record Note feature of the Windows client.
//
// Deploy:  wrangler deploy
// Secret:  wrangler secret put GROQ_API_KEY
// Then set transcribe_worker_url in your ClipBridge config to the
// worker's URL.

export default {
    async fetch(request, env) {
        if (request.method !== 'POST') {
            return new Response('POST audio as multipart form data', { status: 405 });
        }
        const form = await request.formData();
        const upstream = new FormData();
        upstream.append('file', form.get('file'));
        upstream.append('model', form.get('model') || 'whisper-large-v3-turbo');
        const language = form.get('language');
        if (language) upstream.append('language', language);

        const res = await fetch(
            'https://api.groq.com/openai/v1/audio/transcriptions',
            {
                method: 'POST',
                headers: { Authorization: `Bearer ${env.GROQ_API_KEY}` },
                body: upstream,
            }
        );
        return new Response(await res.text(), {
            status: res.status,
            headers: { 'Content-Type': 'application/json' },
        });
    },
};
