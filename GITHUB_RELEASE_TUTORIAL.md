HOW TO PRESS "ONE BUTTON" AND GET A DOWNLOADABLE INSTALLER
==========================================================

What you get at the end: a link other people click to download
Easy-Life-Setup-0.1.0.exe, double-click it, and the app installs.

GitHub does the hard work (building the Windows .exe) on a free
Windows machine in the cloud. You just push the code once, then
click "Run". No building on your PC at all.

--------------------------------------------------------------
PART 1 — Create the GitHub repo (one time, ~2 min)
--------------------------------------------------------------
1. Go to github.com and log in (your account is "nixxioncrac").
2. Click the green "New" button (top-right, next to your avatar),
   or go straight to:  https://github.com/new
3. Repo name:    Easy-Life
4. Visibility:   Public     <- IMPORTANT, so people can download
5. Leave everything else as-is. Click "Create repository".
6. You're now on an empty repo page. KEEP THIS PAGE OPEN.

--------------------------------------------------------------
PART 2 — Put the app's source code into the repo (one time)
--------------------------------------------------------------
Easiest way that needs no terminal typing: upload the files
straight from the browser.

1. On that empty repo page, click the link that says:
   "uploading an existing file"
   (or click Add file -> Upload files)
2. A big drop-zone appears. Drag the FOLDER you just unzipped onto it.
   (The unzipped folder is the one containing "Easy-Life-Windows"
    with README.md, pcrituals/ etc. inside.)
3. GitHub uploads everything. Wait for it to finish.
4. Scroll down, type a message like "initial commit", click
   "Commit changes".

(If a file ever says "too big", it's usually a build artifact —
   skip it, we only need the code.)

--------------------------------------------------------------
PART 3 — Press the build button (this makes your installer)
--------------------------------------------------------------
1. In the same repo, click the green "Actions" tab at the top.
   (If you don't see it, click the "..." menu.)
2. On the left you'll see one workflow called "Release".
   Click it.
3. Click the grey "Run workflow" button (top right).
4. A small form appears:
     - Branch:  main        (leave it)
     - Version to release:  type  0.1.0
5. Click the green "Run workflow".
6. GitHub now opens a fresh Windows computer and builds the app + 
   installer. It takes about 10-20 minutes. A yellow "running"
   dot shows progress. You can watch it, or go do something else.

--------------------------------------------------------------
PART 4 — Get your download link (the payoff)
--------------------------------------------------------------
1. When the run finishes, the dot turns green.
2. Click the "Release" text at the top-right of the page
   (or go to the repo home page and look on the RIGHT side for
    "Releases").
3. You'll see "Easy Life 0.1.0". Click it.
4. Scroll down to "Assets". You'll see four files. Give people:
       Easy-Life-Setup-0.1.0.exe
5. Right-click that file -> "Copy link address". THAT link is
   what you post online / send to people.
   It looks like:
   https://github.com/nixxioncrac/Easy-Life/releases/download/v0.1.0/Easy-Life-Setup-0.1.0.exe
6. Anyone who opens it downloads "Easy-Life-Setup-0.1.0.exe",
   double-clicks, and it installs. Done.

--------------------------------------------------------------
PART 5 — Optional but powerful: one-click updates
--------------------------------------------------------------
The release also made "update.json". If you paste this URL into
your own app (Settings -> Updates box, once):

  https://github.com/nixxioncrac/Easy-Life/releases/latest/download/update.json

...then every future update shows an "Update available" button
INSIDE the app. Users never download a new installer again.

--------------------------------------------------------------
FOR NEXT TIME (every future update, 30 seconds)
--------------------------------------------------------------
New version? Just add one tag and push. The SAME "Release" workflow
runs and makes the new installer + update automatically.

GitHub Desktop (the easy clicky way):
  - after you push a change, or later, just re-open the Actions tab
    and click "Run workflow" with the new version number (e.g. 0.2.0).
    The workflow builds even without a tag — it makes one for you.

That's it. After this one-time setup you never touch a build
again — you just type the new version number and click Run.