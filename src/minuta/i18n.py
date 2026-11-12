"""CLI and setup strings in English and Spanish. Code, logs and files stay in English."""

from __future__ import annotations

_lang = "en"

STRINGS: dict[str, dict[str, str]] = {
    # --- setup ---------------------------------------------------------------------
    "setup.welcome": {
        "en": "minuta setup. Recordings stay on this computer; nothing is sent anywhere unless you add a destination.",
        "es": "Configuración de minuta. Las grabaciones se quedan en esta computadora; no se envía nada a menos "
              "que agregues un destino.",
    },
    "setup.language": {"en": "Language for minuta", "es": "Idioma de minuta"},
    "setup.name": {"en": "Your name, as it should appear in transcripts", "es": "Tu nombre, como debe aparecer en las transcripciones"},
    "setup.data_dir": {"en": "Folder for recordings", "es": "Carpeta para las grabaciones"},
    "setup.audio_format": {"en": "Audio format", "es": "Formato del audio"},
    "setup.format.opus": {"en": "opus: as recorded, smallest (recommended)", "es": "opus: tal como se grabó, el más liviano (recomendado)"},
    "setup.format.mp3": {"en": "mp3: plays everywhere, re-encoded, larger", "es": "mp3: se reproduce en todas partes, recodificado, más pesado"},
    "setup.copy": {
        "en": "Also copy each meeting to another folder? (a Drive, Dropbox, iCloud or Obsidian folder works)",
        "es": "¿Copiar también cada reunión a otra carpeta? (sirve una carpeta de Drive, Dropbox, iCloud u Obsidian)",
    },
    "setup.copy_to": {"en": "Folder to copy meetings to", "es": "Carpeta donde copiar las reuniones"},
    "setup.delete_after": {
        "en": "Delete the local audio once that copy is confirmed?",
        "es": "¿Borrar el audio local cuando la copia esté confirmada?",
    },
    "setup.more": {"en": "Send meetings anywhere else?", "es": "¿Enviar las reuniones a algún otro lado?"},
    "setup.more.none": {"en": "no", "es": "no"},
    "setup.more.webhook": {
        "en": "webhook: POST to a URL (n8n, Zapier, your server), signed",
        "es": "webhook: POST a una URL (n8n, Zapier, tu servidor), firmado",
    },
    "setup.more.command": {
        "en": "command: run a program of yours for each meeting",
        "es": "command: ejecutar un programa tuyo por cada reunión",
    },
    "setup.more.obsidian": {
        "en": "obsidian: one note per meeting in your vault",
        "es": "obsidian: una nota por reunión en tu bóveda",
    },
    "setup.more.rclone": {
        "en": "rclone: Google Drive, Dropbox, OneDrive, S3, SFTP and more",
        "es": "rclone: Google Drive, Dropbox, OneDrive, S3, SFTP y más",
    },
    "setup.more.notion": {
        "en": "notion: one page per meeting in a Notion database",
        "es": "notion: una página por reunión en una base de datos de Notion",
    },
    "setup.webhook.url": {
        "en": "Webhook URL (https, or http://127.0.0.1 / localhost)",
        "es": "URL del webhook (https, o http://127.0.0.1 / localhost)",
    },
    "setup.webhook.key": {
        "en": "Signing key: random text of 16+ characters that your receiver also knows (hidden)",
        "es": "Llave de firma: texto aleatorio de 16+ caracteres que tu receptor también conoce (oculta)",
    },
    "setup.webhook.headers": {
        "en": "Extra header, e.g. 'Authorization: Bearer ...' (hidden, empty for none)",
        "es": "Header extra, p. ej. 'Authorization: Bearer ...' (oculto, vacío si no hace falta)",
    },
    "setup.webhook.audio": {"en": "Attach the audio too?", "es": "¿Adjuntar también el audio?"},
    "setup.command": {
        "en": "Program and arguments, e.g. python3 /path/to/script.py",
        "es": "Programa y argumentos, p. ej. python3 /ruta/al/script.py",
    },
    "setup.obsidian.vault": {"en": "Vault folder", "es": "Carpeta de la bóveda"},
    "setup.obsidian.folder": {"en": "Folder inside the vault", "es": "Carpeta dentro de la bóveda"},
    "setup.dest_added": {"en": "Destination {name} added: {message}", "es": "Destino {name} agregado: {message}"},
    "setup.dest_failed": {
        "en": "Destination {name} not added: {error}",
        "es": "No se agregó el destino {name}: {error}",
    },
    "setup.engine": {"en": "Transcription engine", "es": "Motor de transcripción"},
    "setup.engine.local": {"en": "local: free, runs on this computer", "es": "local: gratis, corre en esta computadora"},
    "setup.engine.vercel": {
        "en": "vercel: paid API (Vercel AI Gateway) with your own key",
        "es": "vercel: API de pago (Vercel AI Gateway) con tu propia llave",
    },
    "setup.vercel_model": {"en": "Model", "es": "Modelo"},
    "setup.api_key": {"en": "Vercel AI Gateway API key (hidden)", "es": "Llave de Vercel AI Gateway (oculta)"},
    "setup.api_key_saved": {"en": "Key saved in: {where}", "es": "Llave guardada en: {where}"},
    "setup.api_key_missing": {
        "en": "No API key given: transcription with vercel will fail until you add one.",
        "es": "No se dio una llave: transcribir con vercel va a fallar hasta que agregues una.",
    },
    "setup.mode": {"en": "When to transcribe", "es": "Cuándo transcribir"},
    "setup.mode.on-demand": {"en": "on demand: run `minuta transcribe` when you want", "es": "a pedido: ejecutas `minuta transcribe` cuando quieras"},
    "setup.mode.auto": {"en": "automatically after each call", "es": "automáticamente después de cada llamada"},
    "setup.max_usd": {"en": "Spending cap per transcription, in US$", "es": "Tope de gasto por transcripción, en US$"},
    "setup.tlang": {
        "en": "Meeting language (e.g. en, es-419; empty = detect)",
        "es": "Idioma de las reuniones (p. ej. en, es-419; vacío = detectar)",
    },
    "setup.saved": {"en": "Configuration saved: {path}", "es": "Configuración guardada: {path}"},
    "setup.service_installed": {"en": "Background service installed: {where}", "es": "Servicio en segundo plano instalado: {where}"},
    "setup.service_failed": {
        "en": "Could not install the background service: {error}. Run `minuta serve` by hand.",
        "es": "No se pudo instalar el servicio en segundo plano: {error}. Ejecuta `minuta serve` a mano.",
    },
    "setup.server_up": {"en": "Local server answering on 127.0.0.1:{port}", "es": "Servidor local respondiendo en 127.0.0.1:{port}"},
    "setup.server_down": {
        "en": "The local server is not answering on 127.0.0.1:{port} yet. `minuta doctor` can tell why.",
        "es": "El servidor local todavía no responde en 127.0.0.1:{port}. `minuta doctor` puede decir por qué.",
    },
    "setup.ext_title": {"en": "Install the browser extension", "es": "Instala la extensión del navegador"},
    "setup.ext_steps": {
        "en": "1. Open {url} in {browser}\n2. Turn on Developer mode (top right)\n3. Click \"Load unpacked\" and "
              "choose this folder:\n   {path}\n4. Click the minuta icon in the toolbar and enter this code: {code}",
        "es": "1. Abre {url} en {browser}\n2. Activa el Modo de desarrollador (arriba a la derecha)\n3. Haz clic en "
              "\"Cargar descomprimida\" y elige esta carpeta:\n   {path}\n4. Haz clic en el ícono de minuta en la barra "
              "y escribe este código: {code}",
    },
    "setup.no_browser": {
        "en": "No Chrome, Edge or Brave found. Install one, then load the extension from:\n   {path}",
        "es": "No se encontró Chrome, Edge ni Brave. Instala uno y carga la extensión desde:\n   {path}",
    },
    "setup.waiting_ext": {
        "en": "Waiting for the extension (up to {seconds} s; Ctrl+C to skip)...",
        "es": "Esperando a la extensión (hasta {seconds} s; Ctrl+C para saltar)...",
    },
    "setup.ext_ok": {"en": "Extension paired and connected.", "es": "Extensión emparejada y conectada."},
    "setup.ext_timeout": {
        "en": "The extension did not connect in time. Pair it later with `minuta pair`.",
        "es": "La extensión no se conectó a tiempo. Emparéjala después con `minuta pair`.",
    },
    "setup.ext_skipped": {
        "en": "Extension step skipped. Pair it later with `minuta pair`.",
        "es": "Se saltó el paso de la extensión. Emparéjala después con `minuta pair`.",
    },
    "setup.done": {
        "en": "Done. Join a Google Meet call and minuta records it. Updates are manual: run `minuta update`, then "
              "reload the extension in the extensions page.",
        "es": "Listo. Entra a una llamada de Google Meet y minuta la graba. Las actualizaciones son manuales: ejecuta "
              "`minuta update` y después recarga la extensión en la página de extensiones.",
    },
    # --- pairing ---------------------------------------------------------------------
    "pair.code": {"en": "Pairing code (valid 10 minutes): {code}", "es": "Código de emparejamiento (vale 10 minutos): {code}"},
    # --- general ---------------------------------------------------------------------------
    "no_config": {"en": "minuta is not set up yet. Run `minuta setup`.", "es": "minuta todavía no está configurada. Ejecuta `minuta setup`."},
    "menu.title": {"en": "What do you want to do?", "es": "¿Qué quieres hacer?"},
    "menu.status": {"en": "Status", "es": "Estado"},
    "menu.list": {"en": "List meetings", "es": "Ver reuniones"},
    "menu.transcribe": {"en": "Transcribe a meeting", "es": "Transcribir una reunión"},
    "menu.destinations": {"en": "Destinations", "es": "Destinos"},
    "menu.retry": {"en": "Retry failed deliveries", "es": "Reintentar envíos fallidos"},
    "menu.doctor": {"en": "Check the installation", "es": "Revisar la instalación"},
    "menu.pair": {"en": "Pair the browser extension", "es": "Emparejar la extensión del navegador"},
    "menu.setup": {"en": "Run setup again", "es": "Volver a configurar"},
    "menu.update": {"en": "Update", "es": "Actualizar"},
    "menu.quit": {"en": "Quit", "es": "Salir"},
    "list.empty": {"en": "No meetings yet.", "es": "Todavía no hay reuniones."},
    "transcribe.pick": {"en": "Which meeting?", "es": "¿Qué reunión?"},
    "transcribe.estimate": {
        "en": "{engine}: about {minutes:.1f} min of speech would be sent, ~US${usd:.3f}.",
        "es": "{engine}: se enviarían unos {minutes:.1f} min de voz, ~US${usd:.3f}.",
    },
    "transcribe.over_cap": {
        "en": "That is over the cap of US${cap:.2f}. Raise it with --max-usd.",
        "es": "Eso pasa del tope de US${cap:.2f}. Súbelo con --max-usd.",
    },
    "transcribe.confirm": {"en": "Spend up to US${usd:.3f}?", "es": "¿Gastar hasta US${usd:.3f}?"},
    "transcribe.need_yes": {
        "en": "This engine costs money. Pass --yes to confirm without a prompt.",
        "es": "Este motor cuesta dinero. Pasa --yes para confirmar sin preguntar.",
    },
    "transcribe.cancelled": {"en": "Cancelled; nothing was sent.", "es": "Cancelado; no se envió nada."},
    "transcribe.done": {"en": "Transcript written to {path}", "es": "Transcripción guardada en {path}"},
    "transcribe.exists": {
        "en": "{id} already has a transcript. Use --force to redo it.",
        "es": "{id} ya tiene transcripción. Usa --force para repetirla.",
    },
    "retry.none": {"en": "Nothing to retry.", "es": "No hay nada que reintentar."},
    "uninstall.confirm": {
        "en": "Remove the background service and the configuration?",
        "es": "¿Quitar el servicio en segundo plano y la configuración?",
    },
    "uninstall.data": {
        "en": "Also delete all recordings and transcripts in {path}?",
        "es": "¿Borrar también todas las grabaciones y transcripciones en {path}?",
    },
    "uninstall.done": {"en": "minuta was removed.", "es": "Se quitó minuta."},
    "uninstall.finishing": {
        "en": "The minuta command is removed a few seconds after this finishes.",
        "es": "El comando minuta se quita unos segundos después de que esto termine.",
    },
    "uninstall.finish_by_hand": {
        "en": "To finish, run: uv tool uninstall minuta",
        "es": "Para terminar, ejecuta: uv tool uninstall minuta",
    },
    "uninstall.kept": {"en": "Recordings kept in {path}", "es": "Las grabaciones quedan en {path}"},
    "update.done": {
        "en": "Updated. Now reload the extension in the extensions page and reload any open Meet tab.",
        "es": "Actualizado. Ahora recarga la extensión en la página de extensiones y recarga las pestañas de Meet abiertas.",
    },
    "update.no_source": {
        "en": "No source folder is recorded (general.app_dir). Re-run the installer to update.",
        "es": "No hay carpeta de origen registrada (general.app_dir). Vuelve a ejecutar el instalador para actualizar.",
    },
    # --- rclone destination ---------------------------------------------------------
    "rclone.intro": {
        "en": "rclone uploads to Google Drive, Dropbox, OneDrive, S3, SFTP and more. minuta uses your rclone "
              "configuration and never sees those credentials.",
        "es": "rclone sube a Google Drive, Dropbox, OneDrive, S3, SFTP y más. minuta usa tu configuración de "
              "rclone y nunca ve esas credenciales.",
    },
    "rclone.pick_remote": {"en": "Where should meetings go?", "es": "¿A dónde van las reuniones?"},
    "rclone.new_drive": {"en": "Connect Google Drive now", "es": "Conectar Google Drive ahora"},
    "rclone.new_other": {
        "en": "Connect another service now (opens `rclone config`)",
        "es": "Conectar otro servicio ahora (abre `rclone config`)",
    },
    "rclone.drive_name": {"en": "Name for the new remote", "es": "Nombre del remote nuevo"},
    "rclone.drive_explain": {
        "en": "Running `rclone config create {remote} drive scope=drive.file`. rclone opens your browser to sign "
              "in to Google; with drive.file it can only see the files it creates.",
        "es": "Ejecutando `rclone config create {remote} drive scope=drive.file`. rclone abre el navegador para "
              "entrar a Google; con drive.file solo ve los archivos que él mismo crea.",
    },
    "rclone.other_explain": {
        "en": "Opening `rclone config`: choose n (new remote), give it a name and follow the questions. "
              "Then q to come back here.",
        "es": "Abriendo `rclone config`: elige n (remote nuevo), ponle un nombre y sigue las preguntas. "
              "Después q para volver aquí.",
    },
    "rclone.folder": {"en": "Folder inside the remote", "es": "Carpeta dentro del remote"},
    "rclone.invalid": {"en": "Not valid: {error}", "es": "No es válido: {error}"},
    "rclone.list_failed": {
        "en": "Could not list the rclone remotes: {error}",
        "es": "No se pudieron listar los remotes de rclone: {error}",
    },
    # --- notion --------------------------------------------------------------------
    "notion.token_help": {
        "en": "Create an internal integration at https://www.notion.so/profile/integrations (capabilities: read, "
              "update and insert content) and copy its Internal Integration Secret.",
        "es": "Crea una integración interna en https://www.notion.so/profile/integrations (permisos: leer, "
              "actualizar e insertar contenido) y copia su Internal Integration Secret.",
    },
    "notion.token": {"en": "Notion integration secret (hidden)", "es": "Secreto de la integración de Notion (oculto)"},
    "notion.how": {"en": "Where should meetings go?", "es": "¿Dónde van las reuniones?"},
    "notion.existing": {"en": "an existing database", "es": "una base de datos que ya existe"},
    "notion.create": {
        "en": "a new database, created inside a page I choose",
        "es": "una base de datos nueva, creada dentro de una página que elijo",
    },
    "notion.share_help": {
        "en": "In Notion, open it, menu ... > Connections, and add the integration. Then paste its link here.",
        "es": "En Notion, ábrela, menú ... > Conexiones, y agrega la integración. Después pega su enlace aquí.",
    },
    "notion.database": {"en": "Link or id of the database", "es": "Enlace o id de la base de datos"},
    "notion.parent": {"en": "Link or id of the page", "es": "Enlace o id de la página"},
    "notion.created": {"en": "Database created: {url}", "es": "Base de datos creada: {url}"},
}


def set_language(lang: str | None) -> None:
    global _lang
    _lang = lang if lang in ("en", "es") else "en"


def language() -> str:
    return _lang


def t(key: str, **kw) -> str:
    entry = STRINGS.get(key)
    if entry is None:
        return key
    text = entry.get(_lang) or entry["en"]
    return text.format(**kw) if kw else text
