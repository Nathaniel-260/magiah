# בניית קובץ ההפעלה (magiah.exe) / Building the Windows EXE

קובץ הפעלה יחיד (`dist/magiah.exe`) שמפעיל את שרת ממשק הסקירה ופותח את הדפדפן.
משתמש שאינו טכני רק לוחץ פעמיים על הקובץ — אין צורך בהתקנת פייתון.

A single-file Windows executable (`dist/magiah.exe`) that starts the review-UI
server and opens the browser. A non-technical user just double-clicks it — no
Python install needed.

---

## עברית — הוראות בנייה

יש לבנות במחשב שיש בו פייתון 3.9+ **וחיבור אינטרנט** (להתקנת PyInstaller אם חסר).

> ## ⚠ אזהרה — בנייה רגילה מוחקת את `dist\magiah_data`
>
> אם הרצתם את ה־exe מתוך `dist\`, תיקיית הנתונים `magiah_data` נוצרה **בתוך
> `dist\`** — ושם יושבות תוצאות הסריקה (`report.db` יכול להיות מאות MB),
> ההחלטות שלכם ב־`ui_review.db` וקובצי ה־CSV.
>
> הדגלים `--noconfirm` ו־`--clean` מוחקים את תוכן `dist\` לפני הבנייה — כלומר
> **הפקודה הרגילה תמחק את כל זה**.
>
> **הפתרון:** בונים לתיקייה נפרדת ומעתיקים רק את קובץ ה־exe פנימה (ראו שלב 3).
> לחלופין — פשוט אל תריצו את ה־exe מתוך `dist\`: העתיקו אותו לתיקייה אחרת
> ותנו לנתונים להיווצר שם.

1. פותחים שורת פקודה בתיקיית הפרויקט (זו שמכילה את `launcher.py` ו־`magiah.spec`).
2. מוודאים ש־PyInstaller מותקן (פעם אחת בלבד):

   ```
   python -X utf8 -m pip install pyinstaller
   ```

3. בונים — **לתיקייה זמנית**, כדי לא לגעת בנתונים שב־`dist\`:

   ```
   python -X utf8 -m PyInstaller magiah.spec --noconfirm ^
       --distpath build_out\dist --workpath build_out\work
   ```

   ואז מעתיקים רק את ה־exe למקומו (כדאי לגבות את הקודם קודם):

   ```
   copy dist\magiah.exe dist\magiah.exe.bak
   copy build_out\dist\magiah.exe dist\magiah.exe
   rmdir /s /q build_out
   ```

   אם `dist\` ריקה מנתונים (בנייה ראשונה, או שה־exe רץ ממקום אחר) אפשר פשוט:

   ```
   python -X utf8 -m PyInstaller magiah.spec --noconfirm --clean
   ```

4. הקובץ המוכן: `dist\magiah.exe`. מעתיקים אותו לכל מקום ומריצים בלחיצה כפולה.

> לפני בנייה — ודאו שה־exe **אינו רץ** (סגרו את חלון השורת־פקודה שלו), אחרת
> Windows ינעל את הקובץ והבנייה תיכשל, ובסיס הנתונים עלול להישאר עם קובץ
> `ui_review.db-journal` תלוי.

> הערה על הפרוקסי (NetFree): אם `pip install pyinstaller` נכשל בגלל סינון תוכן,
> יש לבצע את שלב ההתקנה במחשב אחר עם אינטרנט חופשי, או להעתיק את חבילת
> PyInstaller באופן ידני. את הבנייה עצמה (שלב 3) אפשר להריץ במחשב לא־מקוון.

---

## English — build steps

Build on any machine with Python 3.9+ **and internet** (to install PyInstaller
if it is missing).

> ## ⚠ Warning — a normal build deletes `dist\magiah_data`
>
> If you ran the exe from inside `dist\`, its data folder `magiah_data` was
> created **inside `dist\`** — holding scan output (`report.db` can be hundreds
> of MB), your decisions in `ui_review.db`, and the CSV exports.
>
> `--noconfirm` and `--clean` wipe `dist\` before building, so **the plain
> command destroys all of it**.
>
> **Fix:** build into a separate folder and copy just the exe back (step 3).
> Or simply don't run the exe from `dist\` — copy it elsewhere first and let
> the data folder be created there.

1. Open a terminal in the project root (the folder with `launcher.py` and
   `magiah.spec`).
2. Install PyInstaller once:

   ```
   python -X utf8 -m pip install pyinstaller
   ```

3. Build **into a temporary folder** so nothing in `dist\` is touched:

   ```
   python -X utf8 -m PyInstaller magiah.spec --noconfirm ^
       --distpath build_out\dist --workpath build_out\work
   ```

   Then move just the exe into place (back up the previous one first):

   ```
   copy dist\magiah.exe dist\magiah.exe.bak
   copy build_out\dist\magiah.exe dist\magiah.exe
   rmdir /s /q build_out
   ```

   If `dist\` holds no data (first build, or the exe runs from elsewhere) the
   simple form is fine:

   ```
   python -X utf8 -m PyInstaller magiah.spec --noconfirm --clean
   ```

4. Result: `dist\magiah.exe` — copy it anywhere and double-click to run.

> Make sure the exe is **not running** before building (close its console
> window), or Windows will lock the file and the build will fail — possibly
> leaving a stale `ui_review.db-journal` behind.

The spec bundles the whole `magiah` package plus the SPA static files
(`magiah/webui/static/*`) so the UI loads with no external files.

---

## שימוש בקובץ / Using the exe

- **היכן לשים אותו**: בכל תיקייה. הקובץ עצמאי לחלוטין.
  **מומלץ להעתיק אותו מחוץ ל־`dist\`** — כך בנייה עתידית לא תוכל למחוק את
  הנתונים (ראו האזהרה בראש המסמך).
- **היכן נשמרים הנתונים**: כברירת מחדל נוצרת תיקיית `magiah_data` ליד הקובץ.
  - אפשר לקבוע תיקייה אחרת: משתנה סביבה `MAGIAH_OUT`, או קיצור־דרך עם
    `magiah.exe --out "C:\path\to\data"`.
  - שימו לב: אם ה־exe רץ מתוך `dist\`, הנתונים נשמרים ב־`dist\magiah_data` —
    בדיוק התיקייה שבנייה רגילה מוחקת.
- **הרצת סריקה**: פותחים את הממשק (נפתח לבד בדפדפן), לוחצים על כפתור
  "⚙ ניהול סריקה" ומריצים סריקה חדשה. עם סיום הסריקה הממצאים נטענים ללא צורך
  בהפעלה מחדש.
- **אם משהו משתבש**: חלון השורת־פקודה נשאר פתוח ומציג הודעת שגיאה בעברית.

Default data folder: `magiah_data` next to the exe. Override with the
`MAGIAH_OUT` env var or a `--out <dir>` shortcut argument. The console window is
kept open on error so the Hebrew message stays visible.

Prefer copying the exe **out of `dist\`** before using it, so a later rebuild
can never delete your data — see the warning at the top.
