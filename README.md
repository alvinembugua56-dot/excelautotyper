# Doorman - approve new computers from your Android phone

Doorman is a small website you keep on your phone's Home Screen.
When someone opens Excel Auto Typer on a new PC, a request appears in Doorman
with their computer name and a 4-digit code. You tap **Approve once**,
**Always allow** or **Deny**.

Only you can approve: signing in needs your owner password.

## Deploy on Vercel (about 15 minutes, free Hobby plan)

You need: a GitHub account, a Vercel account (sign up with GitHub), and your PC.

### 1. Make your secrets (on your PC)
1. Open the `ApprovalServer` folder (inside this program's folder). Click its address bar, type `cmd` and press Enter.
2. `pip install cryptography`   (only needed for lock-screen alerts)
3. `python make_keys.py`
   Choose an owner password (this is what you type on your phone).
   It prints your **Environment Variables** and saves them in `doorman-secrets.txt`.
   Keep that file private. Never upload it.

### 2. Put the folder on GitHub
1. On github.com, press **New repository**, name it `doorman`, set it to **Private**, press Create.
2. Press **uploading an existing file**, drag in everything from the `ApprovalServer`
   folder (including the `public` folder), but NOT `doorman-secrets.txt`. Press **Commit**.

### 3. Create the Vercel project
1. On vercel.com press **Add New > Project**, import the `doorman` repository, and press **Deploy**.
   (Vercel recognises Flask by itself. No build settings needed.)
2. When it finishes, open the project > **Storage** > **Create Database** > **Upstash for Redis**
   (free plan), and **connect it to this project**. Vercel adds the database settings for you.
3. Open **Settings > Environment Variables** and add each variable from step 1, one by one
   (`APP_KEY`, `OWNER_PASSWORD`, `SECRET_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT`).
4. Open **Deployments**, press the **...** on the latest one, and choose **Redeploy**
   so the new settings take effect.

### 4. Use the short address
Your site is `https://YOUR-PROJECT.vercel.app` (the short name shown under **Domains**).
Use that one. The long addresses with random letters are protected by Vercel's own login
and won't work for the Windows app.
If the short address also asks for a Vercel login, open **Settings > Deployment Protection**
and make sure production isn't set to "All Deployments" (turn Vercel Authentication off).

### 5. Add Doorman to your Android phone
1. Open the address in **Chrome** and sign in with your owner password.
2. Chrome menu (three dots) > **Add to Home screen** (or **Install app**).
3. Open Doorman from the Home Screen and press **Turn on alerts** > Allow.

### 6. Connect the Windows app
In Excel Auto Typer: **Security > Phone approval > Approval website**
- Website address: `https://YOUR-PROJECT.vercel.app`
- App key: the `APP_KEY` value from step 1
- Press **Test connection** (it should say Connected), then **Save**, then rebuild the `.exe`.

## If something doesn't work
- *"The server has no database yet"*: connect Upstash Redis in the Vercel Storage tab, then redeploy.
- *"Wrong app key"*: the App key in the Windows app must match `APP_KEY` in Vercel exactly.
- *Sign-in says the server isn't set up*: `OWNER_PASSWORD` is missing, add it and redeploy.
- *No lock-screen alerts*: check Chrome notifications are allowed for Doorman, and that all three
  `VAPID_*` variables are set. Requests still appear whenever Doorman is open.
- *First request is slow*: normal after the server has been idle; the Windows app waits up to a minute.

## How it protects you
- A computer can only *ask*. Nothing happens until you tap Approve on your phone.
- Each request shows a code that must match the one on that computer's screen.
- Requests expire after 5 minutes. After 5 wrong passwords, sign-in locks for 10 minutes.
- Keep your owner password and App key private. Someone with the App key can send requests
  to your phone, but they can't approve any.
- Doorman reads the free database about once per 4 seconds while it is open on your phone and
  slows down when idle, to stay inside free limits.

## Other hosts
The same code runs on any host with Python (for example PythonAnywhere or Render):
set the same environment variables (or run `python make_keys.py --config` to make `config.json`)
and run `gunicorn app:app` or `python app.py`. Without Upstash it keeps data in a SQLite file.
