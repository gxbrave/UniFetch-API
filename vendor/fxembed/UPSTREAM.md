# FxEmbed X parser reference

Source: https://github.com/FxEmbed/FxEmbed
Commit: `2e9097877863050749f7ab555ce0c77a8caa70b4`
License: MIT, retained in `LICENSE.md`.

This directory contains the unmodified reference files used to adapt X parsing
to Python. It is not a runnable FxEmbed deployment and contains no Node
dependencies or account credentials.

`yunx_downloader/core.py` implements the guest activation POST,
`TweetResultByRestId` query ID/features/field toggles, tweet visibility/repost
unwrapping, original photo URLs and MP4 variant selection in the existing API
process. Optional X_COOKIE uses an authenticated request with its matching ct0
CSRF value instead of a guest token. Runtime requests go directly to api.x.com;
media is downloaded from pbs.twimg.com/video.twimg.com.

When built-in parsing or media retrieval fails, the Python API may fall back
once to the free https://api.fxtwitter.com/status/{id} endpoint. X account
credentials are never sent to that endpoint.

The upstream web renderer, account pool, client transaction generator,
translation, conversations and other platform providers are not ported. X can
refuse anonymous or authenticated requests; a Cookie is not a guarantee of
access. The query ID and feature flags may need updating when X changes its API.
