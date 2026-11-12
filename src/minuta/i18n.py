"""CLI and setup strings in English and Spanish. Code, logs and files stay in English."""

from __future__ import annotations

import contextlib

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
    "setup.others": {"en": "Others", "es": "Otros"},
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
    "setup.engine.local": {"en": "local (free, on this computer)", "es": "local (gratis, en esta computadora)"},
    "setup.engine.vercel": {
        "en": "Vercel AI Gateway (fast, paid, your own key)",
        "es": "Vercel AI Gateway (rápido, de pago, con tu propia llave)",
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
        "en": "1. Open {url} in {browser}\n2. Turn on Developer mode (top right)\n3. Drag the folder from {fm} onto "
              "that page, or click \"Load unpacked\" and choose it (the path is above)\n4. Click the minuta icon "
              "in the toolbar and type the code below",
        "es": "1. Abre {url} en {browser}\n2. Activa el Modo de desarrollador (arriba a la derecha)\n3. Arrastra la "
              "carpeta desde {fm} a esa página, o haz clic en \"Cargar descomprimida\" y elígela (la ruta está "
              "arriba)\n4. Haz clic en el ícono de minuta en la barra y escribe el código de abajo",
    },
    "setup.no_browser": {
        "en": "No Chrome, Edge or Brave found. Install one, then load the folder above in its extensions page.",
        "es": "No se encontró Chrome, Edge ni Brave. Instala uno y carga la carpeta de arriba en su página de extensiones.",
    },
    "setup.waiting_ext": {
        "en": "Waiting for the extension to pair (up to {seconds} s; Ctrl+C to skip)",
        "es": "Esperando a que la extensión se empareje (hasta {seconds} s; Ctrl+C para saltar)",
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
    "transcribe.exists": {
        "en": "\"{title}\" already has a transcript. Use --force to redo it.",
        "es": "\"{title}\" ya tiene transcripción. Usa --force para repetirla.",
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
    "uninstall.kept": {"en": "Recordings kept in {path}", "es": "Las grabaciones se conservan en {path}"},
    "update.done": {
        "en": "Updated. Now click the reload arrow of minuta in the browser's extensions page and reload any open "
              "Meet tab.",
        "es": "Actualizado. Ahora toca la flecha de recargar de minuta en la página de extensiones del navegador y "
              "recarga las pestañas de Meet abiertas.",
    },
    "update.no_source": {
        "en": "No source folder is recorded (general.app_dir). Re-run the installer to update.",
        "es": "No hay carpeta de origen registrada (general.app_dir). Vuelve a ejecutar el instalador para actualizar.",
    },
    # --- general -------------------------------------------------------------------------
    "answer.yes": {"en": "Yes", "es": "Sí"},
    "prompt.arrows": {"en": "(arrow keys, Enter)", "es": "(flechas y Enter)"},
    "answer.no": {"en": "No", "es": "No"},
    "mark.ok": {"en": "ok", "es": "ok"},
    "mark.warn": {"en": "!", "es": "!"},
    "mark.fail": {"en": "x", "es": "x"},
    "ago": {"en": "{what} ago", "es": "hace {what}"},
    "never": {"en": "never", "es": "nunca"},
    "see_log": {"en": "Details in the log: {path}", "es": "Detalles en el registro: {path}"},
    "fm.mac": {"en": "Finder", "es": "Finder"},
    "fm.windows": {"en": "File Explorer", "es": "el Explorador de archivos"},
    "fm.linux": {"en": "the file manager", "es": "el gestor de archivos"},
    "lookup.none": {"en": "There are no meetings yet.", "es": "Todavía no hay reuniones."},
    "lookup.no_match": {"en": "No meeting matches {ref!r}.", "es": "Ninguna reunión coincide con {ref!r}."},
    "lookup.ambiguous": {
        "en": "{ref!r} matches {n} meetings; use more of the id.",
        "es": "{ref!r} coincide con {n} reuniones; usa más caracteres del id.",
    },
    "meeting.status.recording": {"en": "recording", "es": "grabando"},
    "meeting.status.queued": {"en": "waiting", "es": "en espera"},
    "meeting.status.processing": {"en": "saving", "es": "guardando"},
    "meeting.status.ready": {"en": "ready", "es": "lista"},
    "meeting.status.error": {"en": "error", "es": "error"},
    "meeting.status.discarded": {"en": "discarded", "es": "descartada"},
    "stage.meeting": {"en": "meeting", "es": "reunión"},
    "stage.transcript": {"en": "transcript", "es": "transcripción"},
    "part.audio": {"en": "audio", "es": "audio"},
    "part.transcript": {"en": "transcript", "es": "transcripción"},
    "part.metadata": {"en": "details", "es": "datos"},
    # --- menu ------------------------------------------------------------------------------
    "header.on": {"en": "service on", "es": "servicio activo"},
    "header.off": {"en": "service not answering", "es": "el servicio no responde"},
    "header.paired": {"en": "extension paired", "es": "extensión emparejada"},
    "header.unpaired": {"en": "extension not paired", "es": "extensión sin emparejar"},
    "header.pending": {"en": "{n} to transcribe", "es": "{n} sin transcribir"},
    "header.all_done": {"en": "all transcribed", "es": "todo transcrito"},
    "menu.mode": {"en": "Recording mode", "es": "Modo de grabación"},
    "menu.engine": {"en": "Transcription engine", "es": "Motor de transcripción"},
    "menu.interrupted": {"en": "Stopped. Back to the menu.", "es": "Detenido. De vuelta al menú."},
    # --- status ----------------------------------------------------------------------------
    "status.checking": {"en": "Checking", "es": "Revisando"},
    "status.version": {"en": "Version", "es": "Versión"},
    "status.service": {"en": "Service", "es": "Servicio"},
    "status.service_running": {"en": "installed and running", "es": "instalado y activo"},
    "status.service_stopped": {"en": "installed, not running", "es": "instalado, detenido"},
    "status.service_missing": {"en": "not installed", "es": "no instalado"},
    "status.server": {"en": "Local server", "es": "Servidor local"},
    "status.server_up": {"en": "answering on 127.0.0.1:{port}", "es": "responde en 127.0.0.1:{port}"},
    "status.server_down": {"en": "not answering on 127.0.0.1:{port}", "es": "no responde en 127.0.0.1:{port}"},
    "status.extension": {"en": "Extension", "es": "Extensión"},
    "status.ext_paired": {"en": "paired", "es": "emparejada"},
    "status.ext_seen": {"en": "paired, last seen {ago}", "es": "emparejada, vista por última vez {ago}"},
    "status.ext_unpaired": {"en": "not paired", "es": "sin emparejar"},
    "status.mode": {"en": "Recording", "es": "Grabación"},
    "status.recording": {"en": "Now", "es": "Ahora"},
    "status.recording_now": {"en": "recording {n} call(s)", "es": "grabando {n} llamada(s)"},
    "status.meetings": {"en": "Meetings", "es": "Reuniones"},
    "status.meetings_value": {"en": "{total} ({pending} without a transcript)", "es": "{total} ({pending} sin transcripción)"},
    "status.failed": {"en": "Deliveries", "es": "Envíos"},
    "status.failed_value": {
        "en": "{n} meeting(s) with failed deliveries: use Retry",
        "es": "{n} reunión(es) con envíos fallidos: usa Reintentar",
    },
    "status.data": {"en": "Data folder", "es": "Carpeta de datos"},
    "status.config": {"en": "Configuration", "es": "Configuración"},
    # --- list ------------------------------------------------------------------------------
    "list.col.when": {"en": "Date", "es": "Fecha"},
    "list.col.title": {"en": "Title", "es": "Título"},
    "list.col.duration": {"en": "Length", "es": "Duración"},
    "list.col.status": {"en": "Status", "es": "Estado"},
    "list.col.transcript": {"en": "Transcript", "es": "Transcripción"},
    "list.col.id": {"en": "Id", "es": "Id"},
    "list.transcribed": {"en": "transcribed", "es": "transcrita"},
    "list.not_transcribed": {"en": "no transcript", "es": "sin transcripción"},
    "list.failed_to": {"en": "not delivered to {names}", "es": "no se envió a {names}"},
    "list.hint": {
        "en": "Transcribe one with `minuta transcribe <id>` (the start of the id is enough).",
        "es": "Transcribe una con `minuta transcribe <id>` (basta el comienzo del id).",
    },
    # --- transcribe ------------------------------------------------------------------------
    "transcribe.options": {"en": "Looking at the engines", "es": "Revisando los motores"},
    "transcribe.which_engine": {"en": "Transcribe with", "es": "Transcribir con"},
    "transcribe.engine": {"en": "Engine: {engine}", "es": "Motor: {engine}"},
    "transcribe.free": {"en": "free", "es": "gratis"},
    "transcribe.up_to": {"en": "up to ~US${usd:.3f}", "es": "hasta ~US${usd:.3f}"},
    "transcribe.takes": {"en": "about {time}", "es": "unos {time}"},
    "transcribe.not_installed": {"en": "not installed", "es": "no instalado"},
    "transcribe.measuring": {"en": "Measuring the speech to send", "es": "Midiendo la voz que se enviaría"},
    "transcribe.failed": {"en": "The transcription failed: {error}", "es": "La transcripción falló: {error}"},
    "transcribe.step.audio": {"en": "Preparing the audio", "es": "Preparando el audio"},
    "transcribe.step.model": {"en": "Loading the model {model}", "es": "Cargando el modelo {model}"},
    "transcribe.step.download": {
        "en": "Downloading the model {model}{size}, only the first time",
        "es": "Descargando el modelo {model}{size}, solo la primera vez",
    },
    "transcribe.step.transcribe": {"en": "Transcribing", "es": "Transcribiendo"},
    "transcribe.step.deliver": {"en": "Sending to the destinations", "es": "Enviando a los destinos"},
    "transcribe.done.audio": {"en": "Audio ready", "es": "Audio listo"},
    "transcribe.done.model": {"en": "Model {model} loaded", "es": "Modelo {model} cargado"},
    "transcribe.done.download": {"en": "Model {model} downloaded{size}", "es": "Modelo {model} descargado{size}"},
    "transcribe.done.transcribe": {"en": "Transcribed ({speech} of speech)", "es": "Transcrita ({speech} de voz)"},
    "transcribe.done.deliver": {"en": "Sent to the destinations", "es": "Enviada a los destinos"},
    "summary.title": {"en": "Transcript ready: {title}", "es": "Transcripción lista: {title}"},
    "summary.duration": {"en": "Length", "es": "Duración"},
    "summary.speakers": {"en": "Speakers", "es": "Hablantes"},
    "summary.turns": {"en": "{name} ({n} turns)", "es": "{name} ({n} intervenciones)"},
    "summary.turn": {"en": "{name} (1 turn)", "es": "{name} (1 intervención)"},
    "summary.nobody": {"en": "nobody spoke", "es": "nadie habló"},
    "summary.engine": {"en": "Engine", "es": "Motor"},
    "summary.cost": {"en": "Cost", "es": "Costo"},
    "summary.file": {"en": "File", "es": "Archivo"},
    "summary.also": {"en": "Also in", "es": "También en"},
    "summary.in_destination": {"en": "copy of {name}", "es": "copia de {name}"},
    "summary.in_data": {"en": "data folder", "es": "carpeta de datos"},
    "summary.delivery": {"en": "Delivery", "es": "Envío"},
    "summary.preview": {"en": "First lines:", "es": "Primeras líneas:"},
    "summary.next": {"en": "And now?", "es": "¿Y ahora?"},
    "summary.open": {"en": "Open the transcript", "es": "Abrir la transcripción"},
    "summary.show": {"en": "Show it here", "es": "Mostrarla aquí"},
    "summary.back": {"en": "Back to the menu", "es": "Volver al menú"},
    "summary.opened": {"en": "Opened {path}", "es": "Abierta: {path}"},
    "summary.cannot_open": {"en": "Could not open it; it is in {path}", "es": "No se pudo abrir; está en {path}"},
    # --- destinations ----------------------------------------------------------------------
    "dest.col.name": {"en": "Name", "es": "Nombre"},
    "dest.col.type": {"en": "Type", "es": "Tipo"},
    "dest.col.state": {"en": "State", "es": "Estado"},
    "dest.col.sends": {"en": "Sends", "es": "Qué envía"},
    "dest.col.where": {"en": "Where", "es": "A dónde"},
    "dest.enabled": {"en": "on", "es": "activo"},
    "dest.disabled": {"en": "disabled", "es": "desactivado"},
    "dest.data_folder": {"en": "the data folder only", "es": "solo la carpeta de datos"},
    "dest.none": {"en": "No destinations.", "es": "No hay destinos."},
    "dest.types": {"en": "Types you can add:", "es": "Tipos que puedes agregar:"},
    "dest.add_hint": {
        "en": "Add one with `minuta destinations add <name> --type <type>`.",
        "es": "Agrega uno con `minuta destinations add <nombre> --type <tipo>`.",
    },
    "dest.type.local": {
        "en": "keep meetings in the data folder, optionally copying them to another folder",
        "es": "guarda las reuniones en la carpeta de datos y, si quieres, las copia a otra carpeta",
    },
    "dest.type.webhook": {"en": "POST to a URL (n8n, Zapier, your server), signed", "es": "POST a una URL (n8n, Zapier, tu servidor), firmado"},
    "dest.type.command": {"en": "run a program of yours for each meeting and transcript", "es": "ejecuta un programa tuyo por cada reunión y transcripción"},
    "dest.type.obsidian": {"en": "one note per meeting in your vault", "es": "una nota por reunión en tu bóveda"},
    "dest.type.rclone": {"en": "Google Drive, Dropbox, OneDrive, S3, SFTP and more", "es": "Google Drive, Dropbox, OneDrive, S3, SFTP y más"},
    "dest.type.notion": {"en": "one page per meeting in a Notion database", "es": "una página por reunión en una base de datos de Notion"},
    "dest.name_required": {"en": "A destination name is required.", "es": "Falta el nombre del destino."},
    "dest.unknown_type": {"en": "Unknown type {kind!r}; available: {known}", "es": "Tipo desconocido {kind!r}; disponibles: {known}"},
    "dest.checking": {"en": "Checking {name}", "es": "Revisando {name}"},
    "dest.not_added": {"en": "{name}: not added", "es": "{name}: no se agregó"},
    "dest.check_failed": {
        "en": "{name}: {message} (use --force to save anyway)",
        "es": "{name}: {message} (usa --force para guardarlo igual)",
    },
    "dest.added": {"en": "{name}: added. {message}", "es": "{name}: agregado. {message}"},
    "dest.no_such": {"en": "No destination named {name!r}.", "es": "No hay un destino llamado {name!r}."},
    "dest.removed": {"en": "{name}: removed", "es": "{name}: quitado"},
    "dest.enable_done": {"en": "{name}: enabled", "es": "{name}: activado"},
    "dest.disable_done": {"en": "{name}: disabled", "es": "{name}: desactivado"},
    "dest.testing": {"en": "Checking {name}", "es": "Revisando {name}"},
    "dest.no_live": {"en": "{name}: {kind} has no live test", "es": "{name}: {kind} no tiene prueba en vivo"},
    "dest.live": {"en": "Sending a test to {name}", "es": "Enviando una prueba a {name}"},
    "dest.live_failed": {"en": "{name}: the live test failed: {error}", "es": "{name}: la prueba en vivo falló: {error}"},
    "dest.live_ok": {"en": "{name}: live test ok {url}", "es": "{name}: prueba en vivo correcta {url}"},
    "local.in_place": {"en": "meetings stay in the data folder", "es": "las reuniones quedan en la carpeta de datos"},
    "local.not_a_folder": {"en": "{path} exists and is not a folder", "es": "{path} existe y no es una carpeta"},
    "local.cannot_write": {"en": "cannot write to {path}", "es": "no se puede escribir en {path}"},
    "local.copies_to": {"en": "copies to {path}", "es": "copia a {path}"},
    # --- retry -----------------------------------------------------------------------------
    "retry.sending": {"en": "Sending {n} meeting(s) again", "es": "Enviando de nuevo {n} reunión(es)"},
    "retry.col.meeting": {"en": "Meeting", "es": "Reunión"},
    "retry.col.destination": {"en": "Destination", "es": "Destino"},
    "retry.col.stage": {"en": "What", "es": "Qué"},
    "retry.col.error": {"en": "Error", "es": "Error"},
    # --- pair ------------------------------------------------------------------------------
    "pair.code_label": {"en": "Pairing code:", "es": "Código de emparejamiento:"},
    "pair.valid": {"en": "(valid 10 minutes)", "es": "(vale 10 minutos)"},
    "pair.where": {
        "en": "Click the minuta icon in the browser's toolbar and type it. The extension folder is:",
        "es": "Haz clic en el ícono de minuta en la barra del navegador y escríbelo. La carpeta de la extensión es:",
    },
    "pair.rotated": {
        "en": "New server token: every extension has to pair again.",
        "es": "Token del servidor nuevo: cada extensión tiene que emparejarse otra vez.",
    },
    # --- recording mode --------------------------------------------------------------------
    "mode.auto": {"en": "records every call", "es": "graba todas las llamadas"},
    "mode.ask": {"en": "asks before recording", "es": "pregunta antes de grabar"},
    "mode.manual": {"en": "records only when you ask", "es": "graba solo cuando lo pides"},
    "mode.auto.long": {
        "en": "auto: record every Meet call as soon as you join",
        "es": "auto: grabar cada llamada de Meet en cuanto entras",
    },
    "mode.ask.long": {
        "en": "ask: the browser asks Record / Don't record when a call starts (no answer, no recording)",
        "es": "preguntar: el navegador pregunta Grabar / No grabar al empezar la llamada (sin respuesta, no graba)",
    },
    "mode.manual.long": {
        "en": "manual: record only when you click \"Record this call\" in the extension",
        "es": "manual: grabar solo cuando haces clic en \"Grabar esta llamada\" en la extensión",
    },
    "mode.question": {"en": "When should minuta record?", "es": "¿Cuándo debe grabar minuta?"},
    "mode.current": {"en": "Recording mode: {mode}", "es": "Modo de grabación: {mode}"},
    "mode.saved": {"en": "Recording mode saved: {mode}", "es": "Modo de grabación guardado: {mode}"},
    "setup.recording": {"en": "When should minuta record?", "es": "¿Cuándo debe grabar minuta?"},
    # --- engine ----------------------------------------------------------------------------
    "setup.engine.explain_local": {
        "en": "local: free and private, runs on this computer. Here, about {time} per hour of meeting.",
        "es": "local: gratis y privado, corre en esta computadora. Aquí, unos {time} por hora de reunión.",
    },
    "setup.engine.explain_vercel": {
        "en": "Vercel AI Gateway: fast, paid with your own key (about US${usd:.2f} per hour with whisper). "
              "Only the stretches with speech are sent.",
        "es": "Vercel AI Gateway: rápido, de pago con tu propia llave (unos US${usd:.2f} por hora con whisper). "
              "Solo se envían los tramos con voz.",
    },
    "setup.vercel_preset": {"en": "{name} ({model}), ~US${usd:.2f}/h", "es": "{name} ({model}), ~US${usd:.2f}/h"},
    "setup.api_key_where": {"en": "Create a key at: {url}", "es": "Crea una llave en: {url}"},
    "setup.api_key_keep": {
        "en": "Vercel AI Gateway API key (hidden; empty keeps the one stored)",
        "es": "Llave de Vercel AI Gateway (oculta; vacía conserva la guardada)",
    },
    "setup.api_key_checking": {"en": "Checking the key (costs nothing)", "es": "Revisando la llave (no cuesta nada)"},
    "setup.api_key_ok": {"en": "Key works. Balance: US${balance}", "es": "La llave funciona. Saldo: US${balance}"},
    "setup.api_key_invalid": {
        "en": "Vercel refused that key. Paste it again (empty to skip).",
        "es": "Vercel rechazó esa llave. Pégala otra vez (vacía para saltar).",
    },
    "setup.api_key_unknown": {
        "en": "Could not check the key now ({reason}); it is saved anyway.",
        "es": "No se pudo revisar la llave ahora ({reason}); se guarda igual.",
    },
    "setup.api_key_confirm_note": {
        "en": "Before each paid transcription minuta shows the estimated cost and asks first.",
        "es": "Antes de cada transcripción de pago minuta muestra el costo estimado y pregunta.",
    },
    "engine.saved": {"en": "Transcription engine: {engine}", "es": "Motor de transcripción: {engine}"},
    # --- setup, more -----------------------------------------------------------------------
    "setup.service_installing": {"en": "Installing the background service", "es": "Instalando el servicio en segundo plano"},
    "setup.server_waiting": {"en": "Waiting for the local server", "es": "Esperando al servidor local"},
    "setup.ext_copying": {"en": "Preparing the extension folder", "es": "Preparando la carpeta de la extensión"},
    "setup.ext_folder": {"en": "Extension folder:", "es": "Carpeta de la extensión:"},
    "setup.which_browser": {"en": "Which browser do you use for Meet?", "es": "¿Qué navegador usas para Meet?"},
    "setup.ext_other_browsers": {"en": "In another browser: {list}", "es": "En otro navegador: {list}"},
    "setup.ext_copied": {"en": "Folder path copied to the clipboard.", "es": "Ruta de la carpeta copiada al portapapeles."},
    "setup.ext_folder_opened": {"en": "Folder opened in {fm}.", "es": "Carpeta abierta en {fm}."},
    # --- doctor ----------------------------------------------------------------------------
    "doctor.checking": {"en": "Checking the installation", "es": "Revisando la instalación"},
    "doctor.col.check": {"en": "Check", "es": "Qué"},
    "doctor.col.detail": {"en": "Detail", "es": "Detalle"},
    "doctor.all_good": {"en": "Everything works.", "es": "Todo funciona."},
    "doctor.problems": {"en": "{n} problem(s) to fix.", "es": "{n} problema(s) por resolver."},
    "doctor.name.python": {"en": "Python", "es": "Python"},
    "doctor.name.minuta": {"en": "minuta", "es": "minuta"},
    "doctor.name.ffmpeg": {"en": "ffmpeg", "es": "ffmpeg"},
    "doctor.name.ffprobe": {"en": "ffprobe", "es": "ffprobe"},
    "doctor.name.config": {"en": "configuration", "es": "configuración"},
    "doctor.name.data_folder": {"en": "data folder", "es": "carpeta de datos"},
    "doctor.name.service": {"en": "service", "es": "servicio"},
    "doctor.name.server": {"en": "local server", "es": "servidor local"},
    "doctor.name.extension": {"en": "extension", "es": "extensión"},
    "doctor.name.browser": {"en": "browser", "es": "navegador"},
    "doctor.name.secrets": {"en": "secrets", "es": "secretos"},
    "doctor.name.engine": {"en": "engine {name}", "es": "motor {name}"},
    "doctor.name.destination": {"en": "destination {name}", "es": "destino {name}"},
    "doctor.not_found": {"en": "not found", "es": "no encontrado"},
    "doctor.hint.ffmpeg": {
        "en": "install ffmpeg (brew install ffmpeg, apt install ffmpeg, winget install ffmpeg)",
        "es": "instala ffmpeg (brew install ffmpeg, apt install ffmpeg, winget install ffmpeg)",
    },
    "doctor.hint.setup": {"en": "run `minuta setup`", "es": "ejecuta `minuta setup`"},
    "doctor.hint.data": {"en": "choose another folder in setup", "es": "elige otra carpeta en la configuración"},
    "doctor.service_running": {"en": "installed, running", "es": "instalado, activo"},
    "doctor.service_stopped": {"en": "installed, not running", "es": "instalado, detenido"},
    "doctor.service_missing": {"en": "not installed", "es": "no instalado"},
    "doctor.hint.service": {
        "en": "run `minuta service install` or `minuta setup`",
        "es": "ejecuta `minuta service install` o `minuta setup`",
    },
    "doctor.hint.server": {
        "en": "is another program using the port? change [server] port and run setup again",
        "es": "¿otro programa usa el puerto? cambia [server] port y vuelve a ejecutar la configuración",
    },
    "doctor.ext_seen": {"en": "paired, last seen {seconds} s ago", "es": "emparejada, vista hace {seconds} s"},
    "doctor.hint.extension": {
        "en": "load {path} in the browser and run `minuta pair`",
        "es": "carga {path} en el navegador y ejecuta `minuta pair`",
    },
    "doctor.no_browser": {"en": "no Chrome, Edge or Brave found", "es": "no se encontró Chrome, Edge ni Brave"},
    "doctor.local_missing": {
        "en": "{module} is not installed (reinstall with the 'local' extra)",
        "es": "{module} no está instalado (reinstala con el extra 'local')",
    },
    "doctor.key_stored": {"en": "key stored", "es": "llave guardada"},
    "doctor.key_missing": {"en": "{name} missing", "es": "falta {name}"},
    "doctor.hint.key": {
        "en": "choose Transcription engine in the menu (or `minuta engine`) to store the key",
        "es": "elige Motor de transcripción en el menú (o `minuta engine`) para guardar la llave",
    },
    # --- update, uninstall, service --------------------------------------------------------
    "update.fetching": {"en": "Getting the new version", "es": "Descargando la versión nueva"},
    "update.fetched": {"en": "New version downloaded", "es": "Versión nueva descargada"},
    "update.fetch_failed": {"en": "Could not get the new version", "es": "No se pudo descargar la versión nueva"},
    "update.installing": {"en": "Installing it (can take a minute)", "es": "Instalándola (puede tardar un minuto)"},
    "update.installed": {"en": "Installed", "es": "Instalada"},
    "update.install_failed": {"en": "The installation failed", "es": "La instalación falló"},
    "update.no_uv": {"en": "uv is not installed.", "es": "uv no está instalado."},
    "update.extension_synced": {"en": "Extension folder updated: {path}", "es": "Carpeta de la extensión actualizada: {path}"},
    "update.extension_moved": {
        "en": "The extension now has its own folder, easy to find: \"{new}\". Your browser still loads it from "
              "\"{old}\", which keeps working and is updated too. To switch, remove minuta in the extensions page "
              "and load the new folder (then pair again with `minuta pair`).",
        "es": "La extensión ahora tiene su propia carpeta, fácil de encontrar: \"{new}\". Tu navegador todavía la "
              "carga desde \"{old}\", que sigue funcionando y también se actualiza. Para cambiar, quita minuta en "
              "la página de extensiones y carga la carpeta nueva (luego emparéjala de nuevo con `minuta pair`).",
    },
    "update.restarting": {"en": "Restarting the service", "es": "Reiniciando el servicio"},
    "update.restarted": {"en": "Service restarted", "es": "Servicio reiniciado"},
    "uninstall.service": {"en": "Removing the background service", "es": "Quitando el servicio en segundo plano"},
    "uninstall.service_done": {"en": "Background service removed", "es": "Servicio en segundo plano quitado"},
    "uninstall.secrets_done": {"en": "Stored keys deleted", "es": "Llaves guardadas borradas"},
    "uninstall.config_done": {"en": "Configuration and logs deleted", "es": "Configuración y registros borrados"},
    "uninstall.extension_done": {
        "en": "Extension folder deleted ({path}); remove minuta from the browser's extensions page too",
        "es": "Carpeta de la extensión borrada ({path}); quita minuta también de la página de extensiones",
    },
    "uninstall.data_done": {"en": "Recordings and transcripts deleted: {path}", "es": "Grabaciones y transcripciones borradas: {path}"},
    "uninstall.tool": {"en": "Removing the minuta command", "es": "Quitando el comando minuta"},
    "service.doing.install": {"en": "Installing the service", "es": "Instalando el servicio"},
    "service.doing.uninstall": {"en": "Removing the service", "es": "Quitando el servicio"},
    "service.doing.start": {"en": "Starting the service", "es": "Iniciando el servicio"},
    "service.doing.stop": {"en": "Stopping the service", "es": "Deteniendo el servicio"},
    "service.doing.restart": {"en": "Restarting the service", "es": "Reiniciando el servicio"},
    "service.doing.status": {"en": "Checking the service", "es": "Revisando el servicio"},
    "service.state": {"en": "Service: {state}", "es": "Servicio: {state}"},
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


@contextlib.contextmanager
def forced_language(lang: str):
    """Use `lang` inside the block (`--json` output stays English whatever the CLI's language)."""
    global _lang
    before = _lang
    _lang = lang
    try:
        yield
    finally:
        _lang = before


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
