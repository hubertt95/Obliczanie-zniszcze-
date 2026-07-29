import streamlit as st
import ezdxf
from shapely.geometry import Polygon, LineString, Point
import geopandas as gpd
import pandas as pd
import requests
import shapely.wkt
import io
import os
import tempfile
import concurrent.futures
import plotly.graph_objects as go

st.set_page_config(page_title="Geodezja - Kalkulator Zniszczeń", layout="wide")

st.title("⚡ Geodezja: Kalkulator Zniszczeń Kabla")
st.write("Wrzuć plik DXF, sprawdź natychmiastowy podgląd CAD, a następnie wygeneruj raport i pobierz dane.")

# Inicjalizacja pamięci sesji
if "dxf_data" not in st.session_state:
    st.session_state.dxf_data = None
if "excel_data" not in st.session_state:
    st.session_state.excel_data = None
if "wyniki_df" not in st.session_state:
    st.session_state.wyniki_df = None
if "intersekcja" not in st.session_state:
    st.session_state.intersekcja = None
if "dzialki_gdf" not in st.session_state:
    st.session_state.dzialki_gdf = None
if "zniszczenia_gdf_oryginalne" not in st.session_state:
    st.session_state.zniszczenia_gdf_oryginalne = None
if "kabel_geoms_raw" not in st.session_state:
    st.session_state.kabel_geoms_raw = []
if "zniszczenia_geoms_raw" not in st.session_state:
    st.session_state.zniszczenia_geoms_raw = []

# --- FUNKCJE POMOCNICZE ---

def identify_epsg(x):
    strefa = str(x)[0] 
    if strefa == '5': return 2176
    elif strefa == '6': return 2177
    elif strefa == '7': return 2178
    elif strefa == '8': return 2179
    else: return 2177 

def get_sampled_points(poly, distance=5.0):
    points = [poly.representative_point()]
    boundary = poly.exterior
    length = boundary.length
    if length / distance > 50:
        distance = length / 50.0
    d = 0.0
    while d < length:
        points.append(boundary.interpolate(d))
        d += distance
    return points

# --- FUNKCJE API GUGiK ---

def zapytaj_uldk_xy(x, y):
    url = f"https://uldk.gugik.gov.pl/?request=GetParcelByXY&xy={x},{y}&result=id"
    try:
        odp = requests.get(url, timeout=15)
        if odp.status_code == 200 and odp.text.startswith('0'): 
            return odp.text.split('\n')[1].strip()
    except:
        pass
    return None

def pobierz_dane_dzialki_po_id(id_dzialki, srid):
    url = f"https://uldk.gugik.gov.pl/?request=GetParcelById&id={id_dzialki}&result=geom_wkt,wojewodztwo,powiat,gmina,obreb&srid={srid}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200 and r.text.startswith("0"):
            linie = r.text.strip().split('\n')
            if len(linie) > 1:
                parts = linie[1].split('|')
                if len(parts) >= 5:
                    wkt_czysty = parts[0].split(';', 1)[1] if ';' in parts[0] else parts[0]
                    return {
                        'geometry': shapely.wkt.loads(wkt_czysty),
                        'id_dzialki': id_dzialki,
                        'wojewodztwo': parts[1],
                        'powiat': parts[2],
                        'gmina': parts[3],
                        'obreb': parts[4]
                    }
    except:
        pass
    return None

# --- GŁÓWNA APLIKACJA STREAMLIT ---

uploaded_file = st.file_uploader("Wybierz plik DXF z trasą i zniszczeniami", type=["dxf"])

if uploaded_file is not None:
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp_file:
            tmp_file.write(uploaded_file.getvalue())
            tmp_path = tmp_file.name

        doc = ezdxf.readfile(tmp_path)
        msp = doc.modelspace()
        layers = sorted(list(set([layer.dxf.name for layer in doc.layers])))
        os.remove(tmp_path)
    except Exception as e:
        st.error(f"Nie można odczytać pliku DXF: {e}")
        st.stop()

    st.success("Plik DXF wczytany pomyślnie!")

    st.subheader("Ustawienia warstw i raportu")
    
    kabel_layer = st.selectbox("Wybierz warstwę TRASY KABLA:", layers)
    default_zniszch_idx = layers.index("!!!zniszczenia") if "!!!zniszczenia" in layers else 0
    zniszczenia_layer = st.selectbox("Wybierz warstwę ZNISZCZEŃ:", layers, index=default_zniszch_idx)
    
    format_dzialki = st.selectbox("Format numeru działki:", ["Pełny (np. 143411_4.0001.1261)", "Obręb i Numer (np. 0001.1261)", "Tylko Numer (np. 1261)"])
    format_pow = st.selectbox("Zaokrąglenie powierzchni:", ["2 miejsca po przecinku (np. 11.44)", "1 miejsce po przecinku (np. 11.4)", "Brak - liczby całkowite (np. 11)"])

    # --- WSTĘPNE WBUDOWANIE GEOMETRII DO PODGLĄDU OD RAZU PO WYBORZE WARSTW ---
    kabel_geoms_tmp = []
    for entity in msp.query(f'*[layer=="{kabel_layer}"]'):
        if entity.dxftype() == 'LINE':
            kabel_geoms_tmp.append(LineString([(entity.dxf.start[0], entity.dxf.start[1]), (entity.dxf.end[0], entity.dxf.end[1])]))
        elif entity.dxftype() == 'LWPOLYLINE':
            pts = [(p[0], p[1]) for p in entity.get_points(format='xy')]
            if len(pts) >= 2:
                kabel_geoms_tmp.append(LineString(pts))

    zniszczenia_geoms_tmp = []
    for entity in msp.query(f'*[layer=="{zniszczenia_layer}"]'):
        if entity.dxftype() == 'LWPOLYLINE':
            pts = [(p[0], p[1]) for p in entity.get_points(format='xy')]
            if len(pts) >= 3:
                p = Polygon(pts)
                if p.is_valid:
                    zniszczenia_geoms_tmp.append(p)

    st.session_state.kabel_geoms_raw = kabel_geoms_tmp
    st.session_state.zniszczenia_geoms_raw = zniszczenia_geoms_tmp

    if st.button("🚀 Generuj raport i pobierz dane z GUGiK", type="primary"):
        progress_bar = st.progress(0)
        status_text = st.empty()

        try:
            status_text.text("Analiza geometrii z pliku DXF...")
            progress_bar.progress(10)

            zniszczenia_geoms = st.session_state.zniszczenia_geoms_raw
            epsg_code = 2177

            if zniszczenia_geoms:
                epsg_code = identify_epsg(zniszczenia_geoms[0].exterior.coords[0][0])

            if not zniszczenia_geoms:
                st.error(f"Nie znaleziono zamkniętych polilinii na warstwie '{zniszczenia_layer}'!")
                st.stop()

            st.session_state.zniszczenia_gdf_oryginalne = gpd.GeoDataFrame(geometry=zniszczenia_geoms, crs=f"EPSG:{epsg_code}")
            zniszczenia_gdf_1992 = st.session_state.zniszczenia_gdf_oryginalne.to_crs(epsg=2180)

            status_text.text("Generowanie punktów kontrolnych dla obwiedni...")
            progress_bar.progress(25)

            all_points_with_meta = []
            for idx, poly in enumerate(zniszczenia_gdf_1992.geometry.tolist()):
                pts = get_sampled_points(poly, distance=5.0)
                for pt in pts:
                    all_points_with_meta.append((idx + 1, pt))

            obwiednie_id_dzialki = {i+1: set() for i in range(len(zniszczenia_gdf_1992))}
            
            def check_point(p_idx, pt):
                r_id = zapytaj_uldk_xy(pt.x, pt.y)
                return p_idx, r_id

            status_text.text(f"Odpytywanie serwera GUGiK ({len(all_points_with_meta)} punktów w tle)...")
            progress_bar.progress(40)

            completed = 0
            total_points = len(all_points_with_meta)

            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                futures = [executor.submit(check_point, p_idx, pt) for p_idx, pt in all_points_with_meta]
                for future in concurrent.futures.as_completed(futures):
                    completed += 1
                    p_idx, r_id = future.result()
                    if r_id:
                        obwiednie_id_dzialki[p_idx].add(r_id)
                    current_prog = int(40 + (completed / total_points) * 30)
                    progress_bar.progress(min(current_prog, 70))

            wszystkie_unikalne_id = set()
            for ids in obwiednie_id_dzialki.values():
                wszystkie_unikalne_id.update(ids)

            if not wszystkie_unikalne_id:
                st.error("Skan ukończony, ale GUGiK nie odnalazł działek w tych miejscach.")
                st.stop()

            status_text.text(f"Pobieranie geometrii dla {len(wszystkie_unikalne_id)} unikalnych działek...")
            progress_bar.progress(75)

            dane_dzialek = []
            for id_dz in wszystkie_unikalne_id:
                dane = pobierz_dane_dzialki_po_id(id_dz, epsg_code)
                if dane:
                    dane_dzialek.append(dane)

            status_text.text("Obliczanie przecięć i powierzchni (GIS)...")
            progress_bar.progress(85)

            dzialki_df = pd.DataFrame(dane_dzialek).drop_duplicates(subset=['id_dzialki'])
            st.session_state.dzialki_gdf = gpd.GeoDataFrame(dzialki_df, geometry='geometry', crs=f"EPSG:{epsg_code}")

            st.session_state.dzialki_gdf['pow_dzialki_m2'] = st.session_state.dzialki_gdf.geometry.area
            st.session_state.intersekcja = gpd.overlay(st.session_state.dzialki_gdf, st.session_state.zniszczenia_gdf_oryginalne, how='intersection')
            st.session_state.intersekcja['pow_zniszczenia_m2'] = st.session_state.intersekcja.geometry.area

            st.session_state.wyniki_df = st.session_state.intersekcja.groupby(['id_dzialki', 'wojewodztwo', 'powiat', 'obreb', 'pow_dzialki_m2'])['pow_zniszczenia_m2'].sum().reset_index()

            status_text.text("Generowanie pliku DXF oraz Excel...")
            progress_bar.progress(95)

            def fmt_dzialka(dz_id):
                parts = str(dz_id).split('.')
                if format_dzialki.startswith("Obręb"):
                    return f"{parts[-2]}.{parts[-1]}" if len(parts) >= 2 else str(dz_id)
                elif format_dzialki.startswith("Tylko"):
                    return parts[-1] if len(parts) >= 1 else str(dz_id)
                return str(dz_id)

            def fmt_pow(area):
                if format_pow.startswith("1"):
                    return f"{area:.1f}"
                elif format_pow.startswith("Brak"):
                    return f"{area:.0f}"
                return f"{area:.2f}"

            out_doc = ezdxf.new('R2010')
            out_msp = out_doc.modelspace()
            out_doc.layers.add(name='DZIALKI', color=3)
            out_doc.layers.add(name='ZNISZCZENIA_WYNIK', color=1)
            out_doc.layers.add(name='OPISY', color=7)
            out_doc.layers.add(name='KABEL', color=5)

            for entity in msp.query(f'*[layer=="{kabel_layer}"]'):
                out_msp.add_entity(entity.copy())

            for geom in st.session_state.dzialki_gdf.geometry:
                if isinstance(geom, Polygon):
                    out_msp.add_lwpolyline(list(geom.exterior.coords), dxfattribs={'layer': 'DZIALKI'})

            for idx, row in st.session_state.intersekcja.iterrows():
                if isinstance(row.geometry, Polygon):
                    out_msp.add_lwpolyline(list(row.geometry.exterior.coords), dxfattribs={'layer': 'ZNISZCZENIA_WYNIK', 'color': 1})
                    centroid = row.geometry.centroid
                    # Poprawione formatowanie powierzchni: czyste m2 zamiast kodów systemowych
                    tekst = f"Dz: {fmt_dzialka(row['id_dzialki'])}\nP: {fmt_pow(row['pow_zniszczenia_m2'])} m2"
                    out_msp.add_mtext(tekst, dxfattribs={'layer': 'OPISY', 'insert': (centroid.x, centroid.y), 'char_height': 0.5})

            bbox = st.session_state.zniszczenia_gdf_oryginalne.total_bounds
            tabela_x, tabela_y = bbox[2] + 20, bbox[3] 

            out_msp.add_mtext("ZESTAWIENIE SZCZEGOLOWE ZNISZCZEN", dxfattribs={'insert': (tabela_x, tabela_y), 'char_height': 0.75, 'layer': 'OPISY'})
            y_offset = tabela_y - 1.5
            suma_szczegolowa = 0
            for i, row in st.session_state.intersekcja.iterrows():
                suma_szczegolowa += row['pow_zniszczenia_m2']
                linia_txt = f"Poligon {i+1} | Dz: {fmt_dzialka(row['id_dzialki'])} | Zniszcz: {fmt_pow(row['pow_zniszczenia_m2'])} m2"
                out_msp.add_mtext(linia_txt, dxfattribs={'insert': (tabela_x, y_offset), 'char_height': 0.5, 'layer': 'OPISY'})
                y_offset -= 1.0
            out_msp.add_mtext(f"SUMA CALKOWITA: {fmt_pow(suma_szczegolowa)} m2", dxfattribs={'insert': (tabela_x, y_offset - 0.5), 'char_height': 0.5, 'layer': 'OPISY', 'color': 1})

            tabela2_x = tabela_x + 60
            out_msp.add_mtext("PODSUMOWANIE DLA DZIALEK", dxfattribs={'insert': (tabela2_x, tabela_y), 'char_height': 0.75, 'layer': 'OPISY'})
            y_offset2 = tabela_y - 1.5
            suma_zbiorcza = 0
            for i, row in st.session_state.wyniki_df.iterrows():
                suma_zbiorcza += row['pow_zniszczenia_m2']
                linia_txt = f"Dz: {fmt_dzialka(row['id_dzialki'])} | Lacznie zniszcz: {fmt_pow(row['pow_zniszczenia_m2'])} m2"
                out_msp.add_mtext(linia_txt, dxfattribs={'insert': (tabela2_x, y_offset2), 'char_height': 0.5, 'layer': 'OPISY'})
                y_offset2 -= 1.0
            out_msp.add_mtext(f"SUMA CALKOWITA: {fmt_pow(suma_zbiorcza)} m2", dxfattribs={'insert': (tabela2_x, y_offset2 - 0.5), 'char_height': 0.5, 'layer': 'OPISY', 'color': 1})

            with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp_out:
                out_doc.saveas(tmp_out.name)
                tmp_out_path = tmp_out.name

            with open(tmp_out_path, "rb") as f:
                st.session_state.dxf_data = f.read()
            os.remove(tmp_out_path)

            excel_bytes = io.BytesIO()
            with pd.ExcelWriter(excel_bytes, engine='openpyxl') as writer:
                intersekcja_excel = st.session_state.intersekcja.drop(columns=['geometry'], errors='ignore')
                intersekcja_excel.to_excel(writer, sheet_name='Szczegółowe', index=False)
                st.session_state.wyniki_df.to_excel(writer, sheet_name='Podsumowanie', index=False)
            excel_bytes.seek(0)
            st.session_state.excel_data = excel_bytes.getvalue()

            progress_bar.progress(100)
            status_text.text("Gotowe!")
            st.success("Analiza zakończona sukcesem!")

        except Exception as e:
            st.error(f"Wystąpił błąd podczas przetwarzania: {e}")

# --- INTERAKTYWNY PODGLĄD GRAFICZNY W JAKOŚCI CAD (PLOTLY) ---
if st.session_state.kabel_geoms_raw or st.session_state.zniszczenia_geoms_raw:
    st.subheader("🗺️ Interaktywny podgląd CAD")
    st.info("💡 Na komputerze: użyj scrolla myszy do przybliżania i przeciągnij, aby przesunąć. Na telefonie: użyj uszczypnięcia (pinch-to-zoom) dwoma palcami i przesuń dotykiem.")

    fig = go.Figure()

    # 1. Trasa kabla (zawsze widoczna po wczytaniu DXF)
    for line in st.session_state.kabel_geoms_raw:
        x, y = line.xy
        fig.add_trace(go.Scatter(
            x=list(x), y=list(y),
            mode='lines',
            line=dict(color='blue', width=2),
            name="Trasa kabla"
        ))

    # 2. Zniszczenia z DXF (zawsze widoczne po wczytaniu DXF)
    for idx, poly in enumerate(st.session_state.zniszczenia_geoms_raw):
        x, y = poly.exterior.xy
        fig.add_trace(go.Scatter(
            x=list(x), y=list(y),
            mode='lines',
            line=dict(color='red', width=2),
            fill='toself',
            fillcolor='rgba(255, 0, 0, 0.2)',
            name=f"Zniszczenie #{idx+1}"
        ))

    # 3. Działki z GUGiK (pojawiają się po wygenerowaniu raportu)
    if st.session_state.dzialki_gdf is not None:
        for _, row in st.session_state.dzialki_gdf.iterrows():
            geom = row.geometry
            if geom.geom_type == 'Polygon':
                x, y = geom.exterior.xy
                fig.add_trace(go.Scatter(
                    x=list(x), y=list(y),
                    mode='lines',
                    line=dict(color='green', width=1.5),
                    name=f"Działka: {row['id_dzialki']}",
                    hoverinfo='text',
                    text=f"ID Działki: {row['id_dzialki']}<br>Obręb: {row['obreb']}"
                ))

    fig.update_layout(
        title="Wizualizacja wektorowa CAD",
        xaxis=dict(title="X (metry)", scaleanchor="y", scaleratio=1),
        yaxis=dict(title="Y (metry)"),
        showlegend=True,
        height=700,
        margin=dict(l=20, r=20, t=40, b=20),
        dragmode='pan'  # Domyślne narzędzie to przesuwanie mapy (pan)
    )

    # Konfiguracja wspierająca zoom kółkiem myszy oraz gesty dotykowe (pinch-to-zoom na telefonie)
    st.plotly_chart(
        fig, 
        use_container_width=True, 
        config={
            'scrollZoom': True, 
            'edits': {'shapePosition': True},
            'doubleClick': 'reset',
            'responsive': True
        }
    )

# Wyświetlanie tabeli podglądowej i przycisków pobierania
if st.session_state.wyniki_df is not None:
    st.subheader("👀 Tabela podglądowa zestawienia zniszczeń")
    st.dataframe(st.session_state.wyniki_df, use_container_width=True)

    st.subheader("Pobieranie plików wynikowych")
    col1, col2 = st.columns(2)
    with col1:
        st.download_button("📥 Pobierz wynikowy DXF", data=st.session_state.dxf_data, file_name="Wynik_Geodezja.dxf", mime="application/dxf")
    with col2:
        st.download_button("📊 Pobierz zestawienie Excel", data=st.session_state.excel_data, file_name="Zestawienie_Zniszczen.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
