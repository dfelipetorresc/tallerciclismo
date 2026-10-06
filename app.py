import sqlite3
from datetime import datetime
import requests
import streamlit as st

# ==========================================
# 1. CREDENCIALES DE STRAVA Y BASE DE DATOS
# ==========================================
STRAVA_CLIENT_ID = "143229"
STRAVA_CLIENT_SECRET = "ef4ef0f0e079b6acf6d3b303388fe25249ed0103"
STRAVA_REFRESH_TOKEN = "5d376fe78f24b2a8d11c0ed88ad7893f3f508a8e"

DB_NAME = "taller_bici.db"


# ==========================================
# 2. FUNCIONES DE BASE DE DATOS (SQLITE)
# ==========================================
def get_db_connection():
  conn = sqlite3.connect(DB_NAME)
  conn.row_factory = sqlite3.Row
  return conn


def init_db():
  conn = get_db_connection()
  cursor = conn.cursor()

  # Tabla de Componentes
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS componentes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            bici TEXT NOT NULL,
            km_actuales REAL DEFAULT 0,
            limite_km REAL NOT NULL,
            fecha_instalacion TEXT,
            estado TEXT DEFAULT 'Activo'
        )
    """)

  # Tabla de Historial de Mantenimientos
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS historial (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            componente_id INTEGER,
            fecha TEXT,
            tipo_servicio TEXT NOT NULL,
            km_al_momento REAL,
            notas TEXT,
            FOREIGN KEY (componente_id) REFERENCES componentes (id)
        )
    """)

  # Tabla para controlar qué actividades de Strava ya fueron procesadas
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS strava_actividades (
            id INTEGER PRIMARY KEY,
            nombre TEXT,
            distancia_km REAL,
            fecha TEXT
        )
    """)

  # Carga inicial de componentes para Spiro Freedom y Spiro Mítica
  cursor.execute("SELECT COUNT(*) FROM componentes")
  if cursor.fetchone()[0] == 0:
    componentes_base = [
        # Repuestos Spiro Freedom (Carretera)
        (
            "Cadena 12v",
            "Spiro Freedom",
            0.0,
            3500.0,
            datetime.now().strftime("%Y-%m-%d"),
        ),
        (
            "Pastillas Freno Delanteras",
            "Spiro Freedom",
            0.0,
            2500.0,
            datetime.now().strftime("%Y-%m-%d"),
        ),
        (
            "Pastillas Freno Traseras",
            "Spiro Freedom",
            0.0,
            2000.0,
            datetime.now().strftime("%Y-%m-%d"),
        ),
        (
            "Cambio Cables y Fundas de Freno",
            "Spiro Freedom",
            0.0,
            3000.0,
            datetime.now().strftime("%Y-%m-%d"),
        ),
        (
            "Mantenimiento Caja Centro / Rodamientos",
            "Spiro Freedom",
            0.0,
            5000.0,
            datetime.now().strftime("%Y-%m-%d"),
        ),
        # Repuestos Spiro Mítica (Rodillo)
        (
            "Cadena Entreno Rodillo",
            "Spiro Mítica",
            0.0,
            4500.0,
            datetime.now().strftime("%Y-%m-%d"),
        ),
    ]
    cursor.executemany(
        """
        INSERT INTO componentes (nombre, bici, km_actuales, limite_km, fecha_instalacion)
        VALUES (?, ?, ?, ?, ?)
    """,
        componentes_base,
    )

  conn.commit()
  conn.close()


def agregar_kilometros(bici_nombre, km_a_sumar):
  """Suma km a todos los componentes activos de una bicicleta."""
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute(
      """
        UPDATE componentes 
        SET km_actuales = km_actuales + ? 
        WHERE bici = ? AND estado = 'Activo'
    """,
      (km_a_sumar, bici_nombre),
  )
  conn.commit()
  conn.close()


def registrar_mantenimiento(
    componente_id, tipo_servicio, notas, reiniciar_km=True
):
  """Registra la intervención en el historial y reinicia kilómetros si es cambio."""
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute(
      "SELECT km_actuales FROM componentes WHERE id = ?", (componente_id,)
  )
  comp = cursor.fetchone()
  km_actuales = comp["km_actuales"] if comp else 0
  fecha_hoy = datetime.now().strftime("%Y-%m-%d")

  cursor.execute(
      """
        INSERT INTO historial (componente_id, fecha, tipo_servicio, km_al_momento, notas)
        VALUES (?, ?, ?, ?, ?)
    """,
      (componente_id, fecha_hoy, tipo_servicio, km_actuales, notas),
  )

  if reiniciar_km:
    cursor.execute(
        """
            UPDATE componentes 
            SET km_actuales = 0, fecha_instalacion = ?
            WHERE id = ?
        """,
        (fecha_hoy, componente_id),
    )

  conn.commit()
  conn.close()


# Inicializar la base de datos al arrancar
init_db()


# ==========================================
# 3. LÓGICA DE CONEXIÓN CON STRAVA
# ==========================================
def obtener_access_token():
  """Renueva el Access Token de Strava automáticamente usando el Refresh Token."""
  url = "https://www.strava.com/api/v3/oauth/token"
  payload = {
      "client_id": STRAVA_CLIENT_ID,
      "client_secret": STRAVA_CLIENT_SECRET,
      "refresh_token": STRAVA_REFRESH_TOKEN,
      "grant_type": "refresh_token",
  }
  try:
    res = requests.post(url, data=payload, timeout=8)
    if res.status_code == 200:
      return res.json().get("access_token"), None
    else:
      return (
          None,
          f"Error Strava Auth ({res.status_code}): {res.json().get('message')}",
      )
  except Exception as e:
    return None, f"Error de conexión con Strava: {str(e)}"


def sincronizar_strava():
  """Consulta actividades de Strava, filtra duplicados y distribuye km según la bici."""
  token, err = obtener_access_token()
  if not token:
    st.sidebar.error(err)
    return False, err

  url = "https://www.strava.com/api/v3/athlete/activities"
  headers = {"Authorization": f"Bearer {token}"}

  try:
    res = requests.get(url, headers=headers, params={"per_page": 10}, timeout=8)
    if res.status_code != 200:
      return False, f"Error API Strava ({res.status_code})"

    actividades = res.json()
    if not actividades:
      return True, "No se encontraron actividades en Strava."

    conn = get_db_connection()
    cursor = conn.cursor()

    km_freedom = 0.0
    km_mitica = 0.0
    act_procesadas = 0

    for act in actividades:
      act_id = act["id"]
      cursor.execute(
          "SELECT id FROM strava_actividades WHERE id = ?", (act_id,)
      )

      if cursor.fetchone() is None:
        dist_km = act.get("distance", 0) / 1000.0
        nombre_act = act.get("name", "Rodada")
        fecha_act = act.get("start_date_local", "")[:10]
        tipo_act = act.get("type", "")

        # Asignación automática: Rodillo (VirtualRide/nombre) vs Carretera
        if tipo_act == "VirtualRide" or "rodillo" in nombre_act.lower():
          km_mitica += dist_km
        else:
          km_freedom += dist_km

        cursor.execute(
            """
                    INSERT INTO strava_actividades (id, nombre, distancia_km, fecha)
                    VALUES (?, ?, ?, ?)
                """,
            (act_id, nombre_act, dist_km, fecha_act),
        )

        act_procesadas += 1

    conn.commit()
    conn.close()

    if act_procesadas > 0:
      if km_freedom > 0:
        agregar_kilometros("Spiro Freedom", km_freedom)
      if km_mitica > 0:
        agregar_kilometros("Spiro Mítica", km_mitica)

      return (
          True,
          f"¡Sincronizado! {act_procesadas} rodada(s) nueva(s). (+{km_freedom:.1f}"
          f" km Spiro Freedom / +{km_mitica:.1f} km Spiro Mítica)",
      )
    else:
      return True, "Todas tus rodadas recientes ya estaban sincronizadas."

  except Exception as e:
    return False, f"Error de sincronización: {str(e)}"


# ==========================================
# 4. INTERFAZ GRÁFICA (STREAMLIT)
# ==========================================
st.set_page_config(
    page_title="Taller Digital de Ciclismo", page_icon="🚲", layout="wide"
)

st.title("🔧 Taller Digital & Control de Componentes")
st.caption("Seguimiento de desgaste mecánico y sincronización con Strava")

# Sidebar
st.sidebar.header("⚙️ Menú Principal")

st.sidebar.subheader("🟧 Sincronización Strava")
if st.sidebar.button("🔄 Importar Rodadas Nuevas", use_container_width=True):
  with st.spinner("Conectando con Strava..."):
    exito, msj = sincronizar_strava()
    if exito:
      st.sidebar.success(msj)
      st.rerun()

st.sidebar.markdown("---")

with st.sidebar.expander("🚴 Agregar Salida Manual"):
  bici_sel = st.selectbox("Bicicleta", ["Spiro Freedom", "Spiro Mítica"])
  km_input = st.number_input(
      "Kilómetros", min_value=0.1, max_value=500.0, value=50.0
  )
  if st.button("➕ Sumar Kilómetros"):
    agregar_kilometros(bici_sel, km_input)
    st.success(f"+{km_input} km sumados a {bici_sel}.")
    st.rerun()

with st.sidebar.expander("🆕 Agregar Nuevo Repuesto"):
  with st.form("form_nuevo_comp"):
    nom_comp = st.text_input("Nombre del Repuesto", "Disco de Freno 160mm")
    bici_comp = st.selectbox(
        "Bicicleta", ["Spiro Freedom", "Spiro Mítica"], key="bici_nueva"
    )
    lim_km = st.number_input("Límite Vida Útil (Km)", value=8000, step=1000)
    submitted = st.form_submit_button("Guardar Repuesto")

    if submitted:
      conn = get_db_connection()
      cursor = conn.cursor()
      cursor.execute(
          """
            INSERT INTO componentes (nombre, bici, km_actuales, limite_km, fecha_instalacion)
            VALUES (?, ?, 0, ?, ?)
        """,
          (nom_comp, bici_comp, lim_km, datetime.now().strftime("%Y-%m-%d")),
      )
      conn.commit()
      conn.close()
      st.success(f"'{nom_comp}' agregado correctamente.")
      st.rerun()

# Dashboard Principal
tab1, tab2, tab3 = st.tabs(
    ["📊 Salud de Repuestos", "🛠️ Registrar Mantenimiento", "📜 Historial"]
)

with tab1:
  st.subheader("Estado de Componentes Activos")
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute("SELECT * FROM componentes WHERE estado = 'Activo'")
  componentes = cursor.fetchall()
  conn.close()

  if not componentes:
    st.info("No hay repuestos registrados en el sistema.")
  else:
    for comp in componentes:
      pct_uso = min(1.0, comp["km_actuales"] / comp["limite_km"])
      vida_restante = max(0.0, 100 - (pct_uso * 100))

      col_info, col_bar, col_status = st.columns([3, 4, 2])

      with col_info:
        st.markdown(f"### ⚙️ {comp['nombre']}")
        st.caption(
            f"Bicicleta: **{comp['bici']}** | Instalado:"
            f" {comp['fecha_instalacion']}"
        )

      with col_bar:
        st.write(
            f"Uso acumulado: **{comp['km_actuales']:.1f} km** /"
            f" {comp['limite_km']:.0f} km"
        )
        st.progress(pct_uso)

      with col_status:
        if vida_restante > 30:
          st.success(f"🟢 **{vida_restante:.0f}%** OK")
        elif vida_restante > 10:
          st.warning(f"🟡 **{vida_restante:.0f}%** Revisar")
        else:
          st.error(f"🔴 **{vida_restante:.0f}%** ¡Cambiar!")

      st.divider()

with tab2:
  st.subheader("🛠️ Registrar Servicio Mecánico")
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute(
      "SELECT id, nombre, bici FROM componentes WHERE estado = 'Activo'"
  )
  comps_activos = cursor.fetchall()
  conn.close()

  if comps_activos:
    opciones = {f"{c['nombre']} ({c['bici']})": c["id"] for c in comps_activos}
    comp_sel = st.selectbox("Selecciona Componente:", list(opciones.keys()))
    tipo_serv = st.selectbox(
        "Acción Realizada",
        [
            "Reemplazo / Cambio por Repuesto Nuevo",
            "Mantenimiento Preventivo / Limpieza / Engrase",
            "Ajuste / Calibración de Tensión",
        ],
    )
    notas_serv = st.text_area(
        "Notas del Taller",
        placeholder=(
            "Ej. Se instalaron cables de freno nuevos con funda de teflón."
        ),
    )

    if st.button("⚙️ Guardar Registro en Historial"):
      comp_id = opciones[comp_sel]
      es_cambio = "Reemplazo" in tipo_serv
      registrar_mantenimiento(
          comp_id, tipo_serv, notas_serv, reiniciar_km=es_cambio
      )
      st.success("¡Mantenimiento guardado correctamente!")
      st.rerun()

with tab3:
  st.subheader("📜 Historial de Intervenciones")
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute("""
        SELECT h.fecha, c.nombre, c.bici, h.tipo_servicio, h.km_al_momento, h.notas
        FROM historial h
        JOIN componentes c ON h.componente_id = c.id
        ORDER BY h.id DESC
    """)
  registros = cursor.fetchall()
  conn.close()

  if registros:
    for reg in registros:
      st.markdown(
          f"**{reg['fecha']}** — **{reg['nombre']}** ({reg['bici']}) |"
          f" *{reg['tipo_servicio']}*"
      )
      st.caption(
          f"Km al momento: {reg['km_al_momento']:.1f} km | Notas:"
          f" {reg['notas'] or 'Sin notas'}"
      )
      st.divider()
  else:
    st.info("Sin registros de mantenimiento aún.")
