# בניית קובץ ההפעלה (magiah.exe) / Building the Windows EXE

קובץ הפעלה יחיד (`dist/magiah.exe`) שמפעיל את שרת ממשק הסקירה ופותח את הדפדפן.
משתמש שאינו טכני רק לוחץ פעמיים על הקובץ — אין צורך בהתקנת פייתון.

A single-file Windows executable (`dist/magiah.exe`) that starts the review-UI
server and opens the browser. A non-technical user just double-clicks it — no
Python install needed.

---

## עברית — הוראות בנייה

יש לבנות במחשב שיש בו פייתון 3.9+ **וחיבור אינטרנט** (להתקנת PyInstaller אם חסר).

1. פותחים שורת פקודה בתיקיית הפרויקט (זו שמכילה את `launcher.py` ו־`magiah.spec`).
2. מוודאים ש־PyInstaller מותקן (פעם אחת בלבד):

   ```
   python -X utf8 -m pip install pyinstaller
   ```

3. בונים:

   ```
   python -X utf8 -m PyInstaller magiah.spec --noconfirm --clean
   ```

4. הקובץ המוכן: `dist\magiah.exe`. מעתיקים אותו לכל מקום ומריצים בלחיצה כפולה.

> הערה על הפרוקסי (NetFree): אם `pip install pyinstaller` נכשל בגלל סינון תוכן,
> יש לבצע את שלב ההתקנה במחשב אחר עם אינטרנט חופשי, או להעתיק את חבילת
> PyInstaller באופן ידני. את הבנייה עצמה (שלב 3) אפשר להריץ במחשב לא־מקוון.

---

## English — build steps

Build on any machine with Python 3.9+ **and internet** (to install PyInstaller
if it is missing).

1. Open a terminal in the project root (the folder with `launcher.py` and
   `magiah.spec`).
2. Install PyInstaller once:

   ```
   python -X utf8 -m pip install pyinstaller
   ```

3. Build:

   ```
   python -X utf8 -m PyInstaller magiah.spec --noconfirm --clean
   ```

4. Result: `dist\magiah.exe` — copy it anywhere and double-click to run.

The spec bundles the whole `magiah` package plus the SPA static files
(`magiah/webui/static/*`) so the UI loads with no external files.

---

## שימוש בקובץ / Using the exe

- **היכן לשים אותו**: בכל תיקייה. הקובץ עצמאי לחלוטין.
- **היכן נשמרים הנתונים**: כברירת מחדל נוצרת תיקיית `magiah_data` ליד הקובץ.
  - אפשר לקבוע תיקייה אחרת: משתנה סביבה `MAGIAH_OUT`, או קיצור־דרך עם
    `magiah.exe --out "C:\path\to\data"`.
- **הרצת סריקה**: פותחים את הממשק (נפתח לבד בדפדפן), לוחצים על כפתור
  "⚙ ניהול סריקה" ומריצים סריקה חדשה. עם סיום הסריקה הממצאים נטענים ללא צורך
  בהפעלה מחדש.
- **אם משהו משתבש**: חלון השורת־פקודה נשאר פתוח ומציג הודעת שגיאה בעברית.

Default data folder: `magiah_data` next to the exe. Override with the
`MAGIAH_OUT` env var or a `--out <dir>` shortcut argument. The console window is
kept open on error so the Hebrew message stays visible.
