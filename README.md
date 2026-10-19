# SIDVinyl — frontend Navidrome/Subsonic para Commodore 64

Un vinilo girando con la portada real del álbum, reproduciendo tu biblioteca de
Navidrome (o cualquier servidor compatible con Subsonic) en un C64 con
**Ultimate 64 / Ultimate-II+**, o en **VICE** (enlace serie SwiftLink sobre TCP). Dos modos de audio, conmutables en caliente (`M`):

| Modo | Qué hace |
|------|----------|
| **DIGI** | Streaming real: 8 kHz, 4 bits por el volumen del SID (NMI de CIA2), con noise shaping y normalización. |
| **SID** | Conversión en tiempo real a chiptune: voz 1/2 melodía y armonía (pitch por HPS), voz 3 bajo + batería (kick/snare/hat por flujo espectral). Aproximado, pero reconocible. |

## Arquitectura

```
C64 (PRG, 6502) ──UCI/TCP (Ultimate)──┐
                 └─ACIA 6551 serie (VICE)─┴► bridge (Python) ──HTTP──► Navidrome / Subsonic
                              └─ ffmpeg: decodifica, DIGI o análisis → registros SID
```
El mismo PRG sirve para ambos: al arrancar detecta si existe UCI (`$DF1D` = `$C9`);
si no, usa el enlace serie. En la pantalla inicial `F1` conmuta el enlace manualmente.
El C64 no habla HTTP: la interfaz de comandos de Ultimate (UCI, `$DF1C-$DF1F`) abre
un socket TCP contra el *bridge*, que hace de pasarela (auth Subsonic con token,
`getAlbumList2`, `getAlbum`, `getCoverArt`, `stream`), convierte la portada a 32
fotogramas de etiqueta giratoria en bitmap multicolor y envía el audio.

## Uso

1. **Ultimate**: Settings → *C64 and Cartridge Settings* / *Command Interface* → habilita
   *Command Interface* y conecta la Ultimate a la red.
2. **Bridge** (en un PC/Raspberry con `python3`, `numpy`, `Pillow` y `ffmpeg`):
   ```
   python3 bridge/sidvinyl_bridge.py --server http://navidrome:4533 --user yo --password secreto
   # o variables NAVIDROME_URL / NAVIDROME_USER / NAVIDROME_PASSWORD
   # prueba sin servidor:  python3 bridge/sidvinyl_bridge.py --demo
   ```
   Escucha en el puerto 6464 (`--host`/`--port`; `--insecure` para TLS autofirmado).
3. **PRG**: `c64/sidvinyl.prg` (se carga con `LOAD"*",8,1` / desde el explorador de la Ultimate, y `RUN`).
   La dirección del bridge se teclea en la pantalla inicial. Para fijar la de por defecto:
   ```
   python3 tools/patch_host.py c64/sidvinyl.prg 192.168.1.50:6464
   python3 tools/send_prg.py c64/sidvinyl.prg IP_DE_LA_ULTIMATE      # opcional, vía REST
   ```

### VICE (sin Ultimate)

VICE no emula el UCI, pero sí una ACIA 6551 (SwiftLink) que puede conectarse por TCP al bridge:
```
python3 bridge/sidvinyl_bridge.py --demo            # (o con --server ...)
python3 tools/patch_host.py c64/sidvinyl.prg 127.0.0.1:6464
x64sc -acia1 -acia1mode 1 -acia1base 0xDE00 -acia1irq 0 -myaciadev 0 \
      -rsdev1 127.0.0.1:6464 -rsdev1baud 19200 -autostartprgmode 1 -autostart c64/sidvinyl.prg
```
En serie: 19200 baudios, sin control de flujo hardware → el bridge respeta un control de flujo
por créditos (`CMD_CREDIT`) y el audio DIGI baja a **3,5 kHz** (la ACIA se sondea desde la NMI del
temporizador; a 38400 los robos de ciclos del VIC provocaban pérdida de bytes). SID no se ve afectado.
La portada (≈20 KB) tarda ~11 s en llegar (primer audio ≈20 s en VICE). Con VICE, una conexión del
monitor binario bloquea las teclas. No hay detección de pérdida de conexión en serie.

### Teclas

| Pantalla | Teclas |
|----------|--------|
| Álbumes | `↑/↓` mover (pasa de página en los bordes), `←/→` página, `RETURN` reproducir, `S` orden (A-Z álbum/artista, nuevos, más oídos, aleatorio, favoritos) |
| Reproduciendo | `N`/`→` siguiente, `P`/`←` anterior, `ESPACIO` pausa, `M` modo DIGI/SID, `+`/`-` volumen, `Q`/`STOP` volver a la lista |
Joystick en el puerto 2: direcciones + fuego = RETURN.

## Compilar y probar

```
c64/build.sh                 # requiere cc65 (ca65/ld65); CC65_BIN=/ruta/bin si no está en PATH
python3 tests/test_audio.py  # codificadores DIGI/SID
python3 tests/test_bridge.py # protocolo bridge de extremo a extremo (biblioteca demo)
python3 tests/test_c64.py    # el PRG real en un simulador C64+UCI (py65) contra el bridge real
TRANSPORT=serial python3 tests/test_c64.py   # idem con la ACIA 6551 (detecta overruns)
python3 tests/test_vice.py   # el PRG real en VICE x64sc (Xvfb) vía ACIA/TCP; ver cabecera del script
```
`tools/c64sim.py` emula 6510, VIC-II (IRQ de raster, render texto/bitmap/sprites), CIA1 (teclado),
CIA2 (NMI) y UCI sobre sockets reales; las capturas quedan en `/tmp/sidvinyl_out`.

## Limitaciones / honestidad

- **No probado en una Ultimate real.** Verificado en el simulador y en VICE 3.7.1 (conexión, listado, ambos modos,
  pausa, volumen, siguiente/anterior, volver al menú, PAL). El UCI se implementó según la documentación
  de Ultimate; si algún detalle de timing difiere, `c64/uci.s` es el único sitio a ajustar.
- Modo SID: aproximación por análisis espectral; funciona mejor con música poco densa.
- Audio DIGI a 4 bits/8 kHz: calidad "de época".
- El volumen actúa en el bridge: con audio ya en cola tarda ~1 s en notarse; en pausa el bridge sigue enviando hasta llenar el buffer TCP.
- Solo se muestran 20 álbumes por página, textos transliterados a ASCII (sin acentos).
- PAL y NTSC se detectan automáticamente.

## Protocolo C64 ↔ bridge (resumen)

Comandos C64→bridge: `HELLO 01 (id, vídeo, versión, transporte), LIST 02, PLAY 03, NEXT 04, PREV 05, MODE 07, VOL 08, STOP 09, CREDIT 0A` (solo serie: unidades de 8 bytes).
Paquetes bridge→C64: `[tipo][len lo][len hi][payload]` con tipos HELLO, LIST, TRACK, COVER (32×640 B),
DIGI (nibbles), SID (22 B/frame), STOP, ERROR, VOL, DISC (disco estático + sprites del brazo). Ver `bridge/protocol.py`.
