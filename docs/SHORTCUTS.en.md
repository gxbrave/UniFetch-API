# iPhone Shortcut setup

The backend prepares files. Apple's “Save to Photo Album” action can save videos and still pictures. For a Live Photo, use a third-party Shortcut library that accepts a matching picture and video.

## 1. Set the address and key

Create a Shortcut named “Save high quality media”. Enable “Show in Share Sheet” and accept Text and URLs.

Add two Text values:

| Value | Example |
| --- | --- |
| Server URL | `https://media.example.com` — replace it with your own address |
| Token | The `API_TOKEN` from your server's `.env` |

Use the Shortcut Input as your sharing text. If it is empty, read the clipboard instead.

## 2. Send the text

Use “Get Contents of URL”:

- URL: `Server URL/v1/jobs`
- Method: POST
- Header: `Authorization: Bearer YOUR_TOKEN`
- Request body: JSON, with the key `text` set to your sharing text

Read the returned `poll_url` from the dictionary.

## 3. Check progress

Every two seconds, GET `poll_url` using the same Authorization header.

- `queued` or `running`: wait and check again.
- `failed`: show `error.message` and stop the Shortcut.
- `succeeded`: save each item in `media`, then stop the Shortcut.

Put these branches inside the repeat action. Stop after saving all media; do not start polling again from the saving actions.

You may also set a maximum wait, such as four minutes. If time runs out, show a message and stop; it does not mean the server job has failed.

## 4. Save each media item

| `kind` | Action |
| --- | --- |
| `video` | Read `url`, GET its contents, then use “Save to Photo Album” |
| `image` | Read `url`, GET its contents, then use “Save to Photo Album” |
| `live_photo` | Send the same item's `url` and `motion_url` to your Live Photo Shortcut library |

If your library accepts files rather than URLs, download the picture and video separately, then pass them to its matching inputs. Follow that library's input order and requirements.

Keep download URLs exactly as returned, including `expires` and `sig`. File downloads do not need the API Token.

`motion_photo_url` is for Android Motion Photo galleries. Saving the picture and video separately with Apple's save action does not create an iPhone Live Photo.

## 5. First run

Allow the Shortcut to contact your server and add media to Photos. On mobile data, use a reachable HTTPS domain or VPN; a private LAN address cannot be reached directly.

The API keeps the picture/video pair together in one `live_photo` item. The selected third-party library handles pairing and saving on iOS.
