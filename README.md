# Förskolemorgon – så sätter du upp den

Morgonskärmen uppdateras automatiskt två gånger per vardag: på morgonen med dagens info och på kvällen med morgondagens.
Den visar Unikum-inlägg för barnens grupper, SMHI-väder vid lämning och hämtning, en packlista och lunchen (när den är kopplad).
Allt personligt är krypterat. Sidan kräver ett lösenord som bara du känner till.

Räkna med 20–30 minuter. Gör stegen i ordning.

---

## Steg 1 – Skapa ett GitHub-konto och ett repo

1. Gå till **github.com** och skapa ett gratis konto, om du inte redan har ett.
2. Klicka på **+** uppe till höger och välj **New repository**.
3. Döp repot till `forskolemorgon`, välj **Public** och klicka på **Create repository**.
   *Public behövs för att gratisversionen av GitHub Pages ska fungera. Ingen personlig information hamnar öppet: barnens namn och grupper ligger som hemligheter, och all daglig data är krypterad.*

## Steg 2 – Ladda upp filerna

1. Packa upp zip-filen på din dator.
2. Klicka på länken **uploading an existing file** i det nya repot.
3. Dra in **allt innehåll** i mappen `forskolemorgon`: `docs`, `skript`, `installningar.json`, `klader.json` och `README.md` (filen `workflow-uppdatera.yml` behöver du inte ladda upp). Klicka sedan på **Commit changes**.

Mappen `.github` är dold på de flesta datorer och följer därför oftast inte med. Skapa den filen för hand:

4. Klicka på **Add file → Create new file**.
5. Skriv exakt det här som filnamn: `.github/workflows/uppdatera.yml`
6. Öppna filen `workflow-uppdatera.yml` från zip-filen i en textredigerare (samma innehåll, men synlig), kopiera allt och klistra in. Klicka på **Commit changes**.

## Steg 3 – Lägg in hemligheterna

Gå till **Settings → Secrets and variables → Actions → New repository secret**. Lägg in en hemlighet i taget:

| Namn | Värde |
|---|---|
| `GMAIL_ADRESS` | det nya Gmail-kontot, t.ex. `kjellgren.hemma@gmail.com` |
| `GMAIL_APPLOSENORD` | applösenordet på 16 tecken (mellanslag går bra) |
| `SIDANS_LOSENORD` | ett lösenord du väljer själv, som du skriver in på telefonen |
| `BARN` | `Namn1=Tryggheten Rosa;Namn2=Viljan Gul` (skriv barnens riktiga namn eller smeknamn) |
| `ANTHROPIC_API_KEY` | *valfri*, gör att AI sammanfattar Unikum-inläggen (se nedan) |
| `SKOLMATEN_TOKEN` | *läggs till senare*, när skolmaten.se har skickat din client token |
| `SKOLMATEN_SKOLA` | *läggs till senare*: `forskolan-klavavagen` |

**AI-nyckel (valfri men rekommenderad):** gå till console.anthropic.com, skapa ett konto, fyll på med t.ex. 5 dollar och skapa en API-nyckel. Det räcker i flera år, eftersom varje körning kostar bråkdelar av ett öre.
Utan nyckel fungerar allt ändå, men Unikum-inläggen visas då bara med sin första rad och sorteras inte i "gör" och "bra att veta".

## Steg 4 – Slå på webbsidan

1. Gå till **Settings → Pages**.
2. Under **Branch** väljer du `main` och mappen `/docs`. Klicka på **Save**.
3. Efter en minut syns adressen överst, ungefär `https://dittnamn.github.io/forskolemorgon/`.

## Steg 5 – Kör första gången

1. Gå till fliken **Actions**. Om GitHub frågar, klicka på knappen som slår på workflows.
2. Klicka på **Uppdatera morgonskärmen → Run workflow → Run workflow**.
3. Vänta ungefär en minut tills det blir en grön bock.
   Om det blir ett rött kryss: klicka på körningen, ta en skärmdump av felet och skicka den till Claude.

## Steg 6 – Lägg skärmen på telefonen

1. Öppna adressen från steg 4 i telefonens webbläsare och skriv in ditt `SIDANS_LOSENORD`.
2. **iPhone:** Dela → *Lägg till på hemskärmen*. **Android:** menyn ⋮ → *Lägg till på startskärmen*.

---

## Vardagen

- **Klädlistan:** tryck på "Ändra klädlistan" längst ned på skärmen. Då öppnas `klader.json` på GitHub. Regler: `"regn"`, `"alltid"`, `"under"` eller `"over"` med `"varde"` i grader, eller `"blast"` med `"varde"` i m/s. Klicka på **Commit changes**. Ändringen syns vid nästa uppdatering.
- **Tider:** lämning och hämtning ställer du in i `installningar.json` (hela timmar).
- **Uppdatera direkt:** Actions → Uppdatera morgonskärmen → Run workflow.
- **Tidpunkter:** GitHub kan försena schemalagda körningar 10–30 minuter. Morgonkörningen ligger därför tidigt.
