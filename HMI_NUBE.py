import io
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import altair as alt
import pandas as pd
import requests
import streamlit as st

# ======================= CONFIGURACIÓN GENERAL =======================
# En la nube no existe COM4. El ESP32 envía datos por Wi-Fi a un endpoint HTTP/JSON
# o a una base de datos/servicio (ej. Firebase, ThingSpeak, servidor propio).
DEFAULT_WIFI_URL = st.secrets.get("ESP32_WIFI_URL", "") if hasattr(st, "secrets") else ""
DEFAULT_API_KEY = st.secrets.get("ESP32_API_KEY", "") if hasattr(st, "secrets") else ""
STALE_S = 8  # segundos sin lecturas válidas => falla de comunicación

# COMANDO PARA INICIAR:  py -m streamlit run HMI.py

INSTITUCION = "Universidad de las Fuerzas Armadas ESPE · Extensión Latacunga"
PROYECTO = "Sistema HMI de monitoreo de temperatura, velocidad y corriente"
ASIGNATURA = "Terotecnología"
INTEGRANTES = ["Erick Calahorrano", "Erick Parra"]

PAGINAS = [
    "Carátula",
    "1. Control de temperatura",
    "2. Control de velocidad",
    "3. Control de corriente",
    "4. Visualización general",
    "5. Intervalo P-F y Fiabilidad",
]

VENTANAS = {
    "Últimos 30 s": 30,
    "Último minuto": 60,
    "Últimos 5 min": 300,
    "Últimos 15 min": 900,
    "Todo el historial": None,
}

VARS = {
    "temp": dict(
        col="temp", dato="temperatura", nombre="Temperatura", unidad="°C", dec=2,
        pantalla=PAGINAS[1], sensor="Temperatura", titulo="Temperatura en función del tiempo"
    ),
    "rpm": dict(
        col="rpm", dato="velocidad", nombre="Velocidad angular", unidad="RPM", dec=2,
        pantalla=PAGINAS[2], sensor="Velocidad", titulo="Velocidad angular en función del tiempo"
    ),
    "cor": dict(
        col="cor", dato="corriente", nombre="Corriente", unidad="A", dec=2,
        pantalla=PAGINAS[3], sensor="Corriente", titulo="Corriente en función del tiempo"
    ),
}

# Convención de colores HMI ISA-101
ICO = {0: "●", 1: "▲", 2: "■", 3: "◆"}
NIV_TXT = {0: "NORMAL", 1: "PREALARMA", 2: "ALARMA", 3: "SIN LECTURA"}
NIVEL_VAR = {0: "#6C7A89", 1: "#D97706", 2: "#DC2626", 3: "#7C3AED"}
NAN = float("nan")

# Paleta base para líneas de gráficos Altair
T = dict(line="#3B82F6", ok="#6C7A89", warn="#D97706", alarm="#DC2626", na="#7C3AED", accent="#3B82F6")

st.set_page_config(page_title="HMI Terotecnología", page_icon="⚙️", layout="wide")


# ======================= ADQUISICIÓN WI-FI / HTTP =======================
def a_numero(texto):
    """Convierte texto o número a float. Devuelve None si es 'nan', None o inválido."""
    if texto is None:
        return None
    try:
        valor = float(texto)
    except (ValueError, TypeError):
        return None
    return None if valor != valor else valor


def fmt(v, d=2, suf=""):
    return "—" if v is None or v != v else f"{v:.{d}f}{suf}"


RE_ETQ = {
    "temp": re.compile(r"temp\w*[^:]*:\s*(-?\d+(?:\.\d+)?|nan)", re.IGNORECASE),
    "rpm": re.compile(r"(?:vel|rpm)\w*[^:]*:\s*(-?\d+(?:\.\d+)?|nan)", re.IGNORECASE),
    "cor": re.compile(r"corr\w*[^:]*:\s*(-?\d+(?:\.\d+)?|nan)", re.IGNORECASE),
}


def extraer_de_texto(linea):
    """Extrae (temperatura, velocidad, corriente) de una trama de texto."""
    m = {k: RE_ETQ[k].search(linea) for k in RE_ETQ}
    if all(m.values()):
        return tuple(a_numero(m[k].group(1)) for k in ("temp", "rpm", "cor"))
    numeros = re.findall(r"-?\d+(?:\.\d+)?", linea)
    if len(numeros) >= 3:
        try:
            return tuple(float(x) for x in numeros[:3])
        except ValueError:
            pass
    return None, None, None


def extraer_payload_wifi(payload):
    """Soporta respuestas JSON de ESP32 / Firebase / API o texto plano."""
    if isinstance(payload, dict):
        # Si viene envuelto en {"data": {...}} o un ID de Firebase
        if all(k not in payload for k in ("temp", "temperatura", "rpm", "velocidad", "cor", "corriente")):
            for v in payload.values():
                if isinstance(v, dict):
                    t, r, c = extraer_payload_wifi(v)
                    if any(x is not None for x in (t, r, c)):
                        return t, r, c

        t = a_numero(payload.get("temp", payload.get("temperatura")))
        r = a_numero(payload.get("rpm", payload.get("velocidad", payload.get("vel"))))
        c = a_numero(payload.get("cor", payload.get("corriente", payload.get("current"))))
        return t, r, c

    if isinstance(payload, str):
        return extraer_de_texto(payload)

    return None, None, None


@st.cache_resource
def obtener_estado():
    """Estado compartido + hilo consultor Wi-Fi (se crean una sola vez)."""
    estado = {
        "temperatura": None,
        "velocidad": None,
        "corriente": None,
        "conectado": False,
        "error": None,
        "fuente": "Wi-Fi / Nube",
        "wifi_url": DEFAULT_WIFI_URL,
        "api_key": DEFAULT_API_KEY,
        "poll_s": 2.0,
        "ultima_linea": "Esperando datos por Wi-Fi...",
        "lecturas": 0,
        "t_ultima": 0.0,
        "hist": deque(maxlen=7200),
        "eventos": deque(maxlen=200),
        "prev": {},
        "ack": False,
        "lock": threading.Lock(),
    }

    def receptor_wifi():
        session = requests.Session()
        while True:
            with estado["lock"]:
                url = (estado["wifi_url"] or "").strip()
                api_key = (estado["api_key"] or "").strip()
                intervalo = max(0.5, float(estado["poll_s"]))

            if not url:
                estado["conectado"] = False
                estado["error"] = "Configura la URL Wi-Fi / nube en la barra lateral o en st.secrets"
                time.sleep(intervalo)
                continue

            try:
                headers = {"Accept": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                    headers["X-API-Key"] = api_key

                resp = session.get(url, headers=headers, timeout=4)
                resp.raise_for_status()

                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type or resp.text.strip().startswith("{"):
                    data_obj = resp.json()
                    estado["ultima_linea"] = str(data_obj)[:140]
                    temp, rpm, cor = extraer_payload_wifi(data_obj)
                else:
                    linea = resp.text.strip().splitlines()[-1] if resp.text.strip() else ""
                    estado["ultima_linea"] = linea[:140] if linea else "Respuesta vacía"
                    temp, rpm, cor = extraer_de_texto(linea)

                if temp is None and rpm is None and cor is None:
                    estado["error"] = "Trama recibida sin valores válidos de temp/rpm/cor"
                else:
                    ahora = datetime.now()
                    with estado["lock"]:
                        estado["temperatura"] = temp
                        estado["velocidad"] = rpm
                        estado["corriente"] = cor
                        estado["conectado"] = True
                        estado["error"] = None
                        estado["lecturas"] += 1
                        estado["t_ultima"] = time.time()
                        estado["hist"].append((
                            ahora,
                            NAN if temp is None else temp,
                            NAN if rpm is None else rpm,
                            NAN if cor is None else cor,
                        ))
            except Exception as e:
                estado["conectado"] = False
                estado["error"] = str(e)

            time.sleep(intervalo)

    threading.Thread(target=receptor_wifi, daemon=True).start()
    return estado


DATOS = obtener_estado()


def leer_df():
    with DATOS["lock"]:
        filas = list(DATOS["hist"])
    return pd.DataFrame(filas, columns=["t", "temp", "rpm", "cor"])


# ======================= ALARMAS Y ESTADOS =======================
def evaluar(cfg):
    d = DATOS
    with d["lock"]:
        conectado = d["conectado"]
        t_ult = d["t_ultima"]
        t = d["temperatura"]
        r = d["velocidad"]
        c = d["corriente"]
        fuente = d["fuente"]

    if not (conectado and (time.time() - t_ult) < STALE_S):
        sin = (3, "Sin datos vigentes por Wi-Fi")
        return {"com": (3, f"Sin comunicación ({fuente})"), "temp": sin, "rpm": sin, "cor": sin}

    if t is None:
        nt = (3, "Sin lectura de temperatura")
    elif t > cfg["t_alta"]:
        nt = (2, "Temperatura alta")
    elif cfg["usar_baja"] and t < cfg["t_baja"]:
        nt = (2, "Temperatura baja")
    elif t >= cfg["t_pre"]:
        nt = (1, "Prealarma de temperatura")
    else:
        nt = (0, "Operación normal")

    if r is None:
        nr = (3, "Sin lectura de velocidad")
    elif r > cfg["r_alta"]:
        nr = (2, "Sobrevelocidad")
    elif cfg["paro"] and r <= 0:
        nr = (2, "Eje detenido")
    elif r >= cfg["r_pre"]:
        nr = (1, "Prealarma de velocidad")
    else:
        nr = (0, "Operación normal")

    if c is None:
        nc = (3, "Sin lectura de corriente")
    elif c > cfg["c_alta"]:
        nc = (2, "Sobrecorriente")
    elif cfg["c_usar_baja"] and c < cfg["c_baja"]:
        nc = (2, "Corriente baja")
    elif c >= cfg["c_pre"]:
        nc = (1, "Prealarma de corriente")
    else:
        nc = (0, "Operación normal")

    return {"com": (0, "Comunicación Wi-Fi correcta"), "temp": nt, "rpm": nr, "cor": nc}


def valor_txt(k):
    V = VARS[k]
    return f"{fmt(DATOS[V['dato']], V['dec'])} {V['unidad']}"


def alarmas_activas(est):
    if est["com"][0]:
        return [dict(
            Variable="Comunicación", Severidad=NIV_TXT[3], Condición=est["com"][1],
            Valor=DATOS["fuente"], Pantalla="General"
        )]
    return [dict(
        Variable=VARS[k]["nombre"], Severidad=NIV_TXT[est[k][0]], Condición=est[k][1],
        Valor=valor_txt(k), Pantalla=VARS[k]["pantalla"]
    ) for k in ("temp", "rpm", "cor") if est[k][0]]


def registrar_eventos(est):
    prev = DATOS["prev"]
    nombres = {"temp": "Temperatura", "rpm": "Velocidad angular", "cor": "Corriente", "com": "Comunicación"}
    vals = {"temp": valor_txt("temp"), "rpm": valor_txt("rpm"), "cor": valor_txt("cor"), "com": DATOS["fuente"]}
    for k, (n, txt) in est.items():
        if k != "com" and est["com"][0]:
            continue
        if prev.get(k) != n and not (k not in prev and n == 0):
            DATOS["eventos"].append({
                "Hora": datetime.now().strftime("%H:%M:%S"),
                "Variable": nombres[k],
                "Evento": txt if n else "Retorno a normal",
                "Valor": vals[k],
            })
        prev[k] = n
    if max(v[0] for v in est.values()) == 0:
        DATOS["ack"] = False


def limites(k, cfg):
    if k == "temp":
        L = {
            f"Prealarma ({cfg['t_pre']:g} °C)": (cfg["t_pre"], "warn"),
            f"Alarma alta ({cfg['t_alta']:g} °C)": (cfg["t_alta"], "alarm"),
        }
        if cfg["usar_baja"]:
            L[f"Alarma baja ({cfg['t_baja']:g} °C)"] = (cfg["t_baja"], "accent")
        return L
    if k == "cor":
        L = {
            f"Prealarma ({cfg['c_pre']:g} A)": (cfg["c_pre"], "warn"),
            f"Alarma alta ({cfg['c_alta']:g} A)": (cfg["c_alta"], "alarm"),
        }
        if cfg["c_usar_baja"]:
            L[f"Alarma baja ({cfg['c_baja']:g} A)"] = (cfg["c_baja"], "accent")
        return L
    return {
        f"Prealarma ({cfg['r_pre']:g} RPM)": (cfg["r_pre"], "warn"),
        f"Alarma alta ({cfg['r_alta']:g} RPM)": (cfg["r_alta"], "alarm"),
    }


ACCION = {
    ("temp", 1): "Vigile la tendencia; revise ventilación y carga del equipo.",
    ("temp", 2): "Reduzca la carga o detenga el equipo y revise la refrigeración.",
    ("temp", 3): "Verifique conexión y cableado del sensor.",
    ("rpm", 1): "Vigile la velocidad y compare con el valor nominal del motor.",
    ("rpm", 2): "Verifique el control del motor, la carga y la alimentación.",
    ("rpm", 3): "Verifique la alineación y conexión del sensor.",
    ("cor", 1): "Vigile la carga; compare con la corriente nominal del motor.",
    ("cor", 2): "Reduzca la carga o detenga el equipo; revise sobrecarga o cortocircuito.",
    ("cor", 3): "Verifique conexión y cableado del sensor de corriente.",
}


# ======================= ESTILOS CSS ADAPTATIVOS NATIVOS =======================
CSS = """<style>
.stApp { color: var(--text-color) !important; }
.card {
    background-color: var(--secondary-background-color) !important;
    border: 1px solid rgba(128, 128, 128, 0.2) !important;
    border-radius: 6px; padding: 0.9rem 1.1rem; margin-bottom: 0.7rem;
    color: var(--text-color) !important;
}
.card b, .card strong, .card div, .card p, .card span, .card ul, .card li { color: var(--text-color) !important; }
.card .s, .tile .s, .hmi-sub { color: var(--text-color) !important; opacity: 0.65; }
.tile {
    background-color: var(--secondary-background-color) !important;
    border: 1px solid rgba(128, 128, 128, 0.2) !important;
    border-top: 4px solid var(--c, #6C7A89) !important;
    border-radius: 6px; padding: 0.6rem 0.9rem; margin-bottom: 0.6rem;
    color: var(--text-color) !important;
}
.tile .t {
    color: var(--text-color) !important; opacity: 0.75; font-size: 0.75rem;
    text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600;
}
.tile .v { font-size: 1.8rem; font-weight: 700; line-height: 1.25; color: var(--text-color) !important; }
.tile.a .v { color: var(--c) !important; }
.tile .v small {
    font-size: 0.85rem; color: var(--text-color) !important;
    opacity: 0.65; font-weight: 500; margin-left: 0.3rem;
}
.banner {
    display: flex; justify-content: space-between; align-items: center; gap: 1rem;
    flex-wrap: wrap; padding: 0.75rem 1rem; border-radius: 6px; font-weight: 700;
    margin-bottom: 1rem; border: 1px solid rgba(128, 128, 128, 0.2);
    background-color: var(--secondary-background-color) !important; color: var(--text-color) !important;
}
.banner small { font-weight: 500; font-size: 0.85rem; }
.lvl0 { background-color: var(--secondary-background-color) !important; color: var(--text-color) !important; border-left: 8px solid #6C7A89 !important; }
.lvl1 { background-color: #D97706 !important; color: #FFFFFF !important; border-left: 8px solid #B45309 !important; }
.lvl1 * { color: #FFFFFF !important; }
.lvl2 { background-color: #DC2626 !important; color: #FFFFFF !important; border-left: 8px solid #991B1B !important; }
.lvl2 * { color: #FFFFFF !important; }
.lvl3 { background-color: #7C3AED !important; color: #FFFFFF !important; border-left: 8px solid #5B21B6 !important; }
.lvl3 * { color: #FFFFFF !important; }
@keyframes blink { 50% { opacity: 0.4; } }
.blink { animation: blink 1s infinite; }
.badge {
    display: inline-block; padding: 0.2rem 0.6rem; border-radius: 4px;
    font-weight: 700; font-size: 0.85rem; color: #FFFFFF !important;
    background-color: var(--c, #6C7A89);
}
.hmi-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; color: var(--text-color) !important; }
.hmi-table th {
    background-color: rgba(128, 128, 128, 0.1) !important; text-align: left;
    padding: 0.4rem 0.6rem; border-bottom: 2px solid rgba(128, 128, 128, 0.2) !important;
    color: var(--text-color) !important;
}
.hmi-table td {
    padding: 0.35rem 0.6rem; border-bottom: 1px solid rgba(128, 128, 128, 0.15) !important;
    color: var(--text-color) !important;
}
.hmi-title { font-size: 1.7rem; font-weight: 700; color: var(--text-color) !important; }
.cover { border-left: 8px solid #3B82F6 !important; }
</style>"""


def h(*partes):
    return "".join(partes)


def tile(titulo, valor, unidad="", n=0, sub=""):
    cls = "tile a" if n else "tile"
    return h(
        f'<div class="{cls}" style="--c:{NIVEL_VAR[n]}"><div class="t">{titulo}</div>',
        f'<div class="v">{valor}<small>{unidad}</small></div><div class="s">{sub or "&nbsp;"}</div></div>',
    )


def badge(n, txt=None):
    return f'<span class="badge l{n}" style="--c:{NIVEL_VAR[n]}">{ICO[n]} {txt or NIV_TXT[n]}</span>'


def fila(tiles, n=4):
    for i in range(0, len(tiles), n):
        for c, t in zip(st.columns(n), tiles[i:i + n]):
            c.markdown(t, unsafe_allow_html=True)


def tabla(df):
    if df.empty:
        st.markdown('<div class="card"><span class="s">Sin registros.</span></div>', unsafe_allow_html=True)
    else:
        st.markdown(
            h('<div class="card" style="overflow-x:auto">',
              df.to_html(index=False, classes="hmi-table", border=0, justify="left"), "</div>"),
            unsafe_allow_html=True,
        )


def encabezado(titulo, sub):
    st.markdown(f'<div class="hmi-title">{titulo}</div><div class="hmi-sub">{sub}</div>', unsafe_allow_html=True)


def banner(est):
    n = max(v[0] for v in est.values())
    if n == 0:
        tit, det = "OPERACIÓN NORMAL", "Todas las variables dentro de los límites establecidos."
    else:
        tit = "OPERACIÓN CON ADVERTENCIA" if n == 1 else "OPERACIÓN ANORMAL"
        det = " · ".join(f'{a["Variable"]}: {a["Condición"]} → ver «{a["Pantalla"]}»' for a in alarmas_activas(est))
    blink = " blink" if n >= 2 and not DATOS["ack"] else ""
    st.markdown(f'<div class="banner lvl{n}{blink}"><span>{ICO[n]} {tit}</span><small>{det}</small></div>',
                unsafe_allow_html=True)


# ======================= GRÁFICAS =======================
def mostrar(ch):
    try:
        st.altair_chart(ch, width="stretch", theme="streamlit")
    except TypeError:
        st.altair_chart(ch, use_container_width=True, theme="streamlit")


def tema_alt(ch):
    return ch.configure(background="transparent")


def grafica(df, col, titulo, nombre, unidad, T, lims, opt, rango, n=0, alto=320, mostrar_leyenda=True):
    d = df[["t", col]].dropna()
    if d.empty:
        st.info("Sin datos en la ventana seleccionada.")
        return

    if opt["auto"]:
        lo, hi = float(d[col].min()), float(d[col].max())
        pad = max((hi - lo) * 0.15, {"°C": 1.0, "A": 0.5}.get(unidad, 10.0))
        rng = max(hi - lo, 2 * pad)
        for v, _ in lims.values():
            if lo - rng <= v <= hi + rng:
                lo, hi = min(lo, v), max(hi, v)
        dom = [lo - pad, hi + pad]
    else:
        dom = [float(rango[0]), float(rango[1])]

    esc_y = alt.Scale(domain=dom, nice=False)
    ax_y = alt.Axis(title=f"{nombre} ({unidad})", grid=True)
    ax_x = alt.Axis(title="Tiempo (hh:mm:ss)", format="%H:%M:%S", grid=True)

    pos_x = alt.X("t:T", axis=ax_x)
    pos_y = alt.Y(f"{col}:Q", scale=esc_y, axis=ax_y)

    tip = [
        alt.Tooltip("t:T", title="Hora", format="%H:%M:%S"),
        alt.Tooltip(f"{col}:Q", title=nombre, format=".2f"),
    ]

    capas = [
        alt.Chart(d)
        .mark_line(color=T["line"], strokeWidth=2, clip=True)
        .encode(x=pos_x, y=pos_y, order=alt.Order("t:T"), tooltip=tip),
        alt.Chart(d.tail(1))
        .mark_point(filled=True, size=120, clip=True, color=[T["ok"], T["warn"], T["alarm"], T["na"]][n])
        .encode(x=pos_x, y=pos_y, tooltip=tip),
    ]

    if lims:
        ld = pd.DataFrame([{"v": v, "Límite": k} for k, (v, _) in lims.items()])
        leg_config = alt.Legend(orient="bottom", title=None) if mostrar_leyenda else None
        capas.append(
            alt.Chart(ld)
            .mark_rule(strokeDash=[6, 4], strokeWidth=2, clip=True)
            .encode(
                y=alt.Y("v:Q", scale=esc_y),
                color=alt.Color(
                    "Límite:N",
                    legend=leg_config,
                    scale=alt.Scale(domain=list(lims), range=[T[c] for _, c in lims.values()]),
                ),
            )
        )

    mostrar(tema_alt(alt.layer(*capas).properties(title=titulo, height=alto)))


def histograma(df, col, titulo, nombre, unidad, T):
    d = df[[col]].dropna()
    if d.empty:
        st.info("Sin datos para el histograma.")
        return

    st.markdown(f"**{titulo}**")
    ch = (
        alt.Chart(d)
        .mark_bar(color=T["line"], opacity=0.85)
        .encode(
            x=alt.X(f"{col}:Q", bin=alt.Bin(maxbins=20), title=f"{nombre} ({unidad})"),
            y=alt.Y("count()", title="Frecuencia (muestras)"),
        )
        .properties(height=220)
    )
    mostrar(tema_alt(ch))


def tendencia(df, col, n=30):
    s = df[["t", col]].dropna().tail(n)
    if len(s) < 5:
        return None
    dt = (s["t"].iloc[-1] - s["t"].iloc[0]).total_seconds()
    return None if dt <= 0 else (s[col].iloc[-1] - s[col].iloc[0]) / dt * 60


def csv_bytes(df):
    export_df = df.rename(columns={
        "t": "Hora",
        "temp": "Temperatura (°C)",
        "rpm": "Velocidad (RPM)",
        "cor": "Corriente (A)",
    })
    buf = io.StringIO()
    export_df.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8")


# ======================= PÁGINAS =======================
def pag_caratula(est):
    st.markdown(h(
        '<div class="card cover">',
        f'<div class="s">{INSTITUCION}</div>',
        f'<div class="hmi-title" style="font-size:2.2rem">⚙️ {PROYECTO}</div>',
        f'<div class="hmi-sub">Asignatura: {ASIGNATURA} · {datetime.now():%d/%m/%Y}</div>',
        f'<div class="s">Integrantes: {", ".join(INTEGRANTES)}</div></div>',
    ), unsafe_allow_html=True)

    st.markdown(h(
        '<div class="card"><b>Descripción del sistema</b><br>',
        'Sistema HMI para la supervisión e inspección en tiempo real de <b>temperatura</b>, <b>velocidad angular</b> y <b>corriente</b> ',
        'mediante enlace Wi-Fi hacia la nube. Permite monitorear tendencias, evaluar límites, gestionar alarmas y estimar el <b>Intervalo P-F / RUL</b>.</div>',
    ), unsafe_allow_html=True)

    fila([
        tile("Estado general", badge(max(v[0] for v in est.values())), "", 0, "ver pantalla 4"),
        tile("Temperatura", fmt(DATOS["temperatura"]), "°C", est["temp"][0], est["temp"][1]),
        tile("Velocidad", fmt(DATOS["velocidad"]), "RPM", est["rpm"][0], est["rpm"][1]),
        tile("Corriente", fmt(DATOS["corriente"]), "A", est["cor"][0], est["cor"][1]),
        tile("Enlace Wi-Fi", "OK" if not est["com"][0] else "FALLA", "", est["com"][0], est["com"][1]),
    ], 5)

    c1, c2, c3, c4, c5 = st.columns(5)
    mods = [
        (c1, "1. Control de temperatura", "• Lectura en tiempo real (°C)<br>• Tendencia rápida y márgenes<br>• Historial de eventos"),
        (c2, "2. Control de velocidad", "• Velocidad en RPM y rad/s<br>• Estado del eje y marcha<br>• Historial de eventos"),
        (c3, "3. Control de corriente", "• Corriente RMS en Amperios<br>• Porcentaje de carga<br>• Historial de eventos"),
        (c4, "4. Visualización general", "• Vista consolidada del proceso<br>• Tendencias comparativas<br>• Alarmas globales activas"),
        (c5, "5. Intervalo P-F y Fiabilidad", "• Análisis de curva P-F (CBM)<br>• Estimación RUL de fallo<br>• Registro de degradación"),
    ]
    for c, t, txt in mods:
        c.markdown(f'<div class="card"><b>{t}</b><br><br><span class="s">{txt}</span></div>', unsafe_allow_html=True)

    st.markdown(h(
        '<div class="card"><b>Convención de estados</b><br><br>',
        badge(0), " &nbsp;", badge(1), " &nbsp;", badge(2), " &nbsp;", badge(3), "<br><br>",
        '<span class="s">Diseño de alto rendimiento conforme a estándares HMI (ISA-101): colores reservados para ',
        'alertas y eventos críticos.</span></div>',
    ), unsafe_allow_html=True)


def pag_variable(k, est, df, dfw, cfg, T, opt, rango):
    V = VARS[k]
    col, n, txt = V["col"], est[k][0], est[k][1]
    valor = DATOS[V["dato"]]
    u, dd = V["unidad"], V["dec"]

    encabezado(V["pantalla"], "Supervisión de variable · alarmas y tendencias")
    banner(est)

    c_graf, c_side = st.columns([3, 1.2])
    lims = limites(k, cfg)

    with c_graf:
        grafica(dfw, col, V["titulo"], V["nombre"], u, T, lims, opt, rango, n, alto=320, mostrar_leyenda=True)
        accion = ACCION.get((k, n), "Sin acciones requeridas.")
        st.markdown(
            h(
                '<div class="card" style="margin-top: 0.4rem;">',
                '<b>Alarma de esta variable</b> &nbsp;&nbsp;', badge(n), '<br>',
                f'<b>Condición:</b> {txt}<br>',
                f'<span class="s"><b>Acción recomendada:</b> {accion}</span>',
                '</div>',
            ),
            unsafe_allow_html=True,
        )

    with c_side:
        st.markdown(tile("Lectura actual", fmt(valor, dd), u, n, txt), unsafe_allow_html=True)
        st.markdown(tile("Estado de la señal", badge(n), "", n, txt), unsafe_allow_html=True)
        tnd_val = tendencia(df, col)
        st.markdown(tile("Tendencia actual", fmt(tnd_val, dd), f"{u}/min", 0, "últimas 30 muestras"), unsafe_allow_html=True)

    st.markdown("### 📊 Indicadores secundarios del proceso")
    s = df[col].dropna()
    mn, mx, pr, sd = (s.min(), s.max(), s.mean(), s.std()) if len(s) else (None,) * 4

    tiles_secundarios = [
        tile("Mínimo", fmt(mn, dd), u, 0, "histórico de sesión"),
        tile("Máximo", fmt(mx, dd), u, 0, "histórico de sesión"),
        tile("Promedio", fmt(pr, dd), u, 0, "histórico de sesión"),
        tile("Desviación estándar", fmt(sd, dd), u, 0, "dispersión de la señal"),
        tile("Muestras válidas", str(len(s)), "", 0, f"{DATOS['lecturas']} tramas recibidas"),
        tile("Lecturas inválidas", str(int(df[col].isna().sum())), "", 0, "valores nan descartados"),
    ]

    if k == "temp":
        marg = None if valor is None else cfg["t_alta"] - valor
        tiles_secundarios.append(tile(
            "Margen a alarma alta", fmt(marg, 2), "°C",
            2 if (marg is not None and marg <= 0) else 0, f"umbral {cfg['t_alta']:g} °C"
        ))
    elif k == "rpm":
        en_marcha = None if valor is None else valor > 0
        tiles_secundarios += [
            tile("Frecuencia de giro", fmt(None if valor is None else valor / 60, 2), "Hz", 0, "RPM / 60"),
            tile("Velocidad angular", fmt(None if valor is None else valor * 6.283185 / 60, 2), "rad/s", 0, "ω = 2π·n/60"),
            tile("Tiempo en marcha", fmt((s > 0).mean() * 100 if len(s) else None, 1), "%", 0, "del historial"),
            tile("Estado del eje", "—" if en_marcha is None else ("EN ROTACIÓN" if en_marcha else "DETENIDO"), "", 0, "estado actual"),
        ]
    else:
        marg = None if valor is None else cfg["c_alta"] - valor
        carga = None if (valor is None or not cfg["c_alta"]) else valor / cfg["c_alta"] * 100
        tiles_secundarios += [
            tile("Margen a alarma alta", fmt(marg, 2), "A", 2 if (marg is not None and marg <= 0) else 0, f"umbral {cfg['c_alta']:g} A"),
            tile("Carga respecto al límite", fmt(carga, 1), "%", 0, "corriente / umbral de alarma"),
            tile("Valor RMS", fmt(float((s ** 2).mean() ** 0.5) if len(s) else None, 2), "A", 0, "del historial"),
        ]

    fila(tiles_secundarios, n=4)

    c_umb, c_hist = st.columns([1.2, 3])
    with c_umb:
        st.markdown(h('<div class="card"><b>Umbrales configurados</b><ul>',
                      "".join(f"<li>{x}</li>" for x in lims), "</ul></div>"), unsafe_allow_html=True)
        st.download_button(
            "⬇️ Exportar historial (CSV)",
            key=f"dl_{k}_{PAGINAS.index(V['pantalla'])}",
            mime="text/csv",
            file_name=f"historial_{k}_{datetime.now():%Y%m%d_%H%M%S}.csv",
            data=csv_bytes(df),
        )
    with c_hist:
        histograma(dfw, col, f"Distribución de lecturas – {V['nombre']}", V["nombre"], u, T)

    c_tabla1, c_tabla2 = st.columns([1.5, 2.5])
    with c_tabla1:
        st.markdown("**Últimas lecturas**")
        if len(df):
            ult = df.tail(8).iloc[::-1]
            tabla(pd.DataFrame({
                "Hora": ult["t"].dt.strftime("%H:%M:%S"),
                f"{V['nombre']} ({u})": ult[col].map(lambda x: fmt(x, dd)),
            }))
        else:
            tabla(pd.DataFrame())

    with c_tabla2:
        st.markdown("**Registro de eventos de esta variable**")
        ev = [e for e in list(DATOS["eventos"])[::-1] if e["Variable"] == V["nombre"]][:8]
        tabla(pd.DataFrame(ev))


def ir(i):
    st.session_state["pidx"] = i


def pag_general(est, dfw, cfg, T, opt, rangos):
    encabezado("4. Visualización general", "Supervisión conjunta del proceso: estado, tendencias y alarmas")
    banner(est)
    fila([
        tile("Temperatura", fmt(DATOS["temperatura"]), "°C", est["temp"][0], est["temp"][1]),
        tile("Velocidad angular", fmt(DATOS["velocidad"]), "RPM", est["rpm"][0], est["rpm"][1]),
        tile("Corriente", fmt(DATOS["corriente"]), "A", est["cor"][0], est["cor"][1]),
        tile("Enlace Wi-Fi", "OK" if not est["com"][0] else "FALLA", "", est["com"][0], est["com"][1]),
    ], 4)

    st.markdown(
        '<div style="display: flex; gap: 1.5rem; align-items: center; justify-content: flex-end; margin-top: 0.5rem; margin-bottom: 0.5rem; font-size: 0.85rem;">'
        '<b>Convención de umbrales:</b> '
        '<span style="color: #D97706; font-weight: bold;">╌╌ Prealarma (Naranja)</span> &nbsp;&nbsp;'
        '<span style="color: #DC2626; font-weight: bold;">╌╌ Alarma Alta (Rojo)</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3)
    for c, k, i in ((c1, "temp", 1), (c2, "rpm", 2), (c3, "cor", 3)):
        V = VARS[k]
        with c:
            grafica(dfw, V["col"], V["titulo"], V["nombre"], V["unidad"], T, limites(k, cfg), opt,
                    rangos[k], est[k][0], alto=320, mostrar_leyenda=False)
            st.markdown(badge(est[k][0]), unsafe_allow_html=True)
            st.button(
                f"Ir a {PAGINAS[i]} →",
                key=f"go{i}",
                type="primary" if est[k][0] else "secondary",
                on_click=ir,
                args=(i,),
            )

    act = alarmas_activas(est)
    st.markdown("**Alarmas activas**")
    if act:
        tabla(pd.DataFrame(act))
        if not DATOS["ack"] and st.button("✔ Reconocer alarma", key="ack"):
            DATOS["ack"] = True
    else:
        st.markdown(f'<div class="card">{badge(0)} &nbsp;Sin alarmas activas.</div>', unsafe_allow_html=True)
    st.markdown("**Registro de eventos recientes**")
    tabla(pd.DataFrame(list(DATOS["eventos"])[::-1][:12]))


# ======================= PÁGINA 5: INTERVALO P-F Y FIABILIDAD =======================
def pag_pf(est, df, dfw, cfg, T):
    encabezado("5. Intervalo P-F y Fiabilidad", "Análisis de Mantenimiento Basado en la Condición (CBM) y estimación RUL")
    banner(est)

    st.markdown(
        '<div class="card"><b>📐 Intervalo P-F en Terotecnología</b><br>'
        'El <b>Punto P (Fallo Potencial)</b> es el instante en el que se detecta el inicio de la degradación (Prealarma). '
        'El <b>Punto F (Fallo Funcional)</b> es el límite donde el equipo deja de cumplir su función de manera segura (Alarma). '
        'El <b>Intervalo P-F</b> es la ventana de tiempo disponible para ejecutar acciones preventivas antes del fallo total.</div>',
        unsafe_allow_html=True,
    )

    vars_pf = [
        ("temp", "Temperatura", "°C", cfg["t_pre"], cfg["t_alta"]),
        ("rpm", "Velocidad angular", "RPM", cfg["r_pre"], cfg["r_alta"]),
        ("cor", "Corriente", "A", cfg["c_pre"], cfg["c_alta"]),
    ]

    c1, c2, c3 = st.columns(3)
    cols_c = [c1, c2, c3]
    eventos_pf = []

    for idx, (k, nombre, unidad, p_lim, f_lim) in enumerate(vars_pf):
        val = DATOS[VARS[k]["dato"]]
        col = VARS[k]["col"]
        s = df[["t", col]].dropna()

        if val is None or val != val:
            estado_pf = "Sin lectura"
            n_st = 3
            rul_txt = "—"
        elif val >= f_lim:
            estado_pf = "Fallo Funcional (F)"
            n_st = 2
            rul_txt = "0.0 min (Fallo alcanzado)"
        elif val >= p_lim:
            estado_pf = "Fallo Potencial (P)"
            n_st = 1
            tnd = tendencia(df, col)
            if tnd is not None and tnd > 0:
                rul_min = (f_lim - val) / tnd
                rul_txt = f"{max(0.0, rul_min):.1f} min estimado"
            else:
                rul_txt = "Tendencia estable / sin aceleración"
        else:
            estado_pf = "Operación Normal (< P)"
            n_st = 0
            rul_txt = "Sin degradación activa"

        t_p, t_f = None, None
        if not s.empty:
            p_mask = s[col] >= p_lim
            f_mask = s[col] >= f_lim
            if p_mask.any():
                t_p = s[p_mask]["t"].iloc[0]
            if f_mask.any():
                t_f = s[f_mask]["t"].iloc[0]

        if t_p is not None and t_f is not None and t_f >= t_p:
            dur_pf = (t_f - t_p).total_seconds() / 60.0
            dur_txt = f"{dur_pf:.2f} min"
        elif t_p is not None:
            dur_pf_parcial = (datetime.now() - t_p).total_seconds() / 60.0
            dur_txt = f"{dur_pf_parcial:.2f} min (en desarrollo)"
        else:
            dur_txt = "Sin anomalía en la sesión"

        with cols_c[idx]:
            st.markdown(
                tile(f"Salud: {nombre}", estado_pf, "", n_st, f"Umbral P: {p_lim:g} {unidad} | F: {f_lim:g} {unidad}"),
                unsafe_allow_html=True,
            )
            st.markdown(
                tile(f"RUL Estimado ({nombre})", rul_txt, "", 1 if n_st == 1 else (2 if n_st == 2 else 0),
                     f"Intervalo P-F de Sesión: {dur_txt}"),
                unsafe_allow_html=True,
            )

        if t_p is not None:
            eventos_pf.append({
                "Variable": nombre,
                "Hora Fallo Potencial (P)": t_p.strftime("%H:%M:%S"),
                "Hora Fallo Funcional (F)": t_f.strftime("%H:%M:%S") if t_f else "No alcanzado",
                "Duración P-F": dur_txt,
                "Estado Activo": estado_pf,
            })

    st.markdown("---")
    st.markdown("### 📈 Inspección de la Curva de Degradación (Curva P-F)")

    var_sel = st.selectbox("Seleccione la variable a inspeccionar:", [v[1] for v in vars_pf], index=0, key="pf_var_sel")
    k_sel = [v[0] for v in vars_pf if v[1] == var_sel][0]
    V = VARS[k_sel]
    col_sel = V["col"]
    u_sel = V["unidad"]
    p_lim_sel = [v[3] for v in vars_pf if v[0] == k_sel][0]
    f_lim_sel = [v[4] for v in vars_pf if v[0] == k_sel][0]

    c_g, c_info = st.columns([3, 1.2])

    with c_g:
        lims_pf = {
            f"Punto P - Fallo Potencial ({p_lim_sel:g} {u_sel})": (p_lim_sel, "warn"),
            f"Punto F - Fallo Funcional ({f_lim_sel:g} {u_sel})": (f_lim_sel, "alarm"),
        }
        grafica(dfw, col_sel, f"Curva de Degradación P-F – {var_sel}", V["nombre"], u_sel, T, lims_pf, OPT,
                RANGOS[k_sel], est[k_sel][0], alto=320, mostrar_leyenda=True)

        st.markdown(
            f'<div class="card" style="margin-top: 0.4rem;"><b>Estrategia de Mantenimiento Recomendada</b><br>'
            f'• <b>Zona Normal (&lt; {p_lim_sel:g} {u_sel}):</b> Monitoreo de rutina.<br>'
            f'• <b>Zona P-F ({p_lim_sel:g} a {f_lim_sel:g} {u_sel}):</b> Planificar intervención inmediata, reajustar carga o ventilación.<br>'
            f'• <b>Zona F (&gt; {f_lim_sel:g} {u_sel}):</b> Paro de emergencia obligatorio.</div>',
            unsafe_allow_html=True,
        )

    with c_info:
        val_sel = DATOS[V["dato"]]
        st.markdown(tile("Lectura Actual", fmt(val_sel, V["dec"]), u_sel, est[k_sel][0]), unsafe_allow_html=True)
        tnd_sel = tendencia(df, col_sel)
        st.markdown(tile("Velocidad de Degradación", fmt(tnd_sel, 2), f"{u_sel}/min", 0, "últimas 30 muestras"), unsafe_allow_html=True)
        st.markdown(tile("Estado P-F", badge(est[k_sel][0]), "", est[k_sel][0], est[k_sel][1]), unsafe_allow_html=True)

    st.markdown("### 📋 Registro de Transiciones P-F Detectadas en la Sesión")
    if eventos_pf:
        tabla(pd.DataFrame(eventos_pf))
    else:
        st.markdown('<div class="card"><span class="s">No se han registrado transiciones a Fallo Potencial (P) en la sesión actual. El equipo opera dentro de los márgenes de diseño.</span></div>', unsafe_allow_html=True)


# ======================= BARRA LATERAL =======================
st.sidebar.markdown("## ⚙️ HMI Terotecnología")
_sel = st.sidebar.radio("Navegación", PAGINAS, index=st.session_state.get("pidx", 0))
st.session_state["pidx"] = PAGINAS.index(_sel)
pagina = _sel

st.markdown(CSS, unsafe_allow_html=True)

with st.sidebar.expander("🌐 Enlace Wi-Fi / Nube", expanded=not bool(DATOS["wifi_url"])):
    url_in = st.text_input("URL JSON / Endpoint ESP32", value=DATOS["wifi_url"], placeholder="https://.../lecturas.json")
    key_in = st.text_input("Token / API Key (opcional)", value=DATOS["api_key"], type="password")
    poll_in = st.slider("Intervalo de consulta (s)", min_value=1.0, max_value=15.0, value=float(DATOS["poll_s"]), step=0.5)
    with DATOS["lock"]:
        DATOS["wifi_url"] = url_in.strip()
        DATOS["api_key"] = key_in.strip()
        DATOS["poll_s"] = float(poll_in)
    if DATOS["error"]:
        st.caption(f"⚠️ Estado Wi-Fi: {DATOS['error']}")

with st.sidebar.expander("📈 Gráficas"):
    _v = st.selectbox("Ventana de tiempo", list(VENTANAS), index=2)
    _auto = st.checkbox("Escala automática", value=True)
    r_t = (st.number_input("Temperatura mín (°C)", value=0.0, disabled=_auto),
           st.number_input("Temperatura máx (°C)", value=60.0, disabled=_auto))
    r_r = (st.number_input("Velocidad mín (RPM)", value=0.0, disabled=_auto),
           st.number_input("Velocidad máx (RPM)", value=4000.0, disabled=_auto))
    r_c = (st.number_input("Corriente mín (A)", value=0.0, disabled=_auto),
           st.number_input("Corriente máx (A)", value=10.0, disabled=_auto))

with st.sidebar.expander("🚨 Umbrales y alarmas"):
    cfg = {
        "t_pre": st.number_input("Prealarma temperatura (°C)", value=30.0),
        "t_alta": st.number_input("Alarma alta temperatura (°C)", value=60.0),
        "usar_baja": st.checkbox("Habilitar alarma de temperatura baja", value=False),
        "t_baja": st.number_input("Alarma baja temperatura (°C)", value=10.0),
        "r_pre": st.number_input("Prealarma velocidad (RPM)", value=2700.0),
        "r_alta": st.number_input("Alarma alta velocidad (RPM)", value=3000.0),
        "paro": st.checkbox("Alarma por eje detenido (0 RPM)", value=False),
        "c_pre": st.number_input("Prealarma corriente (A)", value=4.0),
        "c_alta": st.number_input("Alarma alta corriente (A)", value=5.0),
        "c_usar_baja": st.checkbox("Habilitar alarma de corriente baja", value=False),
        "c_baja": st.number_input("Alarma baja corriente (A)", value=0.2),
    }

if st.sidebar.button("🗑️ Limpiar historial"):
    with DATOS["lock"]:
        DATOS["hist"].clear()
    DATOS["eventos"].clear()

OPT = {"auto": _auto}
RANGOS = {"temp": r_t, "rpm": r_r, "cor": r_c}
VENTANA_S = VENTANAS[_v]


# ======================= PANEL PRINCIPAL =======================
@st.fragment(run_every=2)
def panel(pagina, cfg, T):
    est = evaluar(cfg)
    registrar_eventos(est)
    df = leer_df()
    dfw = df if (VENTANA_S is None or df.empty) else df[df["t"] >= datetime.now() - timedelta(seconds=VENTANA_S)]
    if pagina == PAGINAS[0]:
        pag_caratula(est)
    elif pagina == PAGINAS[1]:
        pag_variable("temp", est, df, dfw, cfg, T, OPT, RANGOS["temp"])
    elif pagina == PAGINAS[2]:
        pag_variable("rpm", est, df, dfw, cfg, T, OPT, RANGOS["rpm"])
    elif pagina == PAGINAS[3]:
        pag_variable("cor", est, df, dfw, cfg, T, OPT, RANGOS["cor"])
    elif pagina == PAGINAS[4]:
        pag_general(est, dfw, cfg, T, OPT, RANGOS)
    else:
        pag_pf(est, df, dfw, cfg, T)


panel(pagina, cfg, T)