import streamlit as st
import ezdxf
from shapely.geometry import Polygon, LineString, Point
import geopandas as gpd
import pandas as pd
import requests
import shapely.wkt
import io
import zipfile
import concurrent.futures

st.set_page_config(page_title="Geodezja - Kalkulator Zniszczeń", layout="centered")

st.title("⚡ Geodezja: Kalkulator Zniszczeń Kabla")
st.write("Wrzuć plik DXF, wybierz warstwy i pobierz gotowy raport oraz DXF bezpośrednio w przeglądarce.")

# Funkcje pomocnicze
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

# Krok 1: Wgranie pliku
uploaded_file = st.file_uploader("Wybierz plik DXF z trasą i zniszczeniami", type=["dxf"])

if uploaded_file is not None:
    try:
        # Bezpieczna konwersja przesłanego pliku na bajty dla ezdxf.read[cite: 11]
        bytes_data = uploaded_file.getvalue()
        if isinstance(bytes_data, str):
            bytes_data = bytes_data.encode("utf-8", errors="ignore")
        doc = ezdxf.read(io.BytesIO(bytes_data))
        msp = doc.modelspace()
        layers = sorted(list(set([layer.dxf.name for layer in doc.layers])))
    except Exception as e:
        st.error(f"Nie można odczytać pliku DXF: {e}")
        st.stop()

    st.success("Plik DXF wczytany pomyślnie!")

    # Krok 2: Wybór ustawień
    st.subheader("Ustawienia warstw i raportu")
    
    kabel_layer = st.selectbox("Wybierz warstwę TRASY KABLA:", layers)
    
    # Domyślne zaznaczenie lub wpisanie warstwy zniszczeń
    default_zniszczenia = "!!!zniszczenia" if "!!!zniszczenia" in layers else layers[0]
    zniszczenia_layers_input = st.text_input("Warstwy ZNISZCZEŃ (oddziel przecinkami, jeśli kilka):", value=default_zniszczenia)
    
    format_dzialki = st.selectbox("Format numeru działki:", ["Pełny (np. 143411_4.0001.1261)", "Obręb i Numer (np. 0001.1261)", "Tylko Numer (np. 1261)"])
    format_pow = st.selectbox("Zaokrąglenie powierzchni:", ["2 miejsca po przecinku (np. 11.44)", "1 miejsce po przecinku (np. 11.4)", "Brak - liczby całkowite (np. 11)"])

    if st.button("🚀 Generuj raport i pliki wynikowe", type="primary"):
        with st.spinner("Przetwarzanie geometrii i pobieranie danych z GUGiK..."):
            try:
                layers_zniszczenia = [l.strip() for l in zniszczenia_layers_input.split(',') if l.strip()]
                zniszczenia_geoms = []
                epsg_code = 2177

                for lay in layers_zniszczenia:
                    obiekty = list(msp.query(f'*[layer=="{lay}"]'))
                    for entity in obiekty:
                        if entity.dxftype() == 'LWPOLYLINE':
                            points = [(p[0], p[1]) for p in entity.get_points(format='xy')]
                            if len(points) >= 3:
                                poly = Polygon(points)
                                if poly.is_valid:
                                    zniszczenia_geoms.append(poly)
                                    if len(zniszczenia_geoms) == 1:
                                        epsg_code = identify_epsg(points[0][0])

                if not zniszczenia_geoms:
                    st.error("Nie znaleziono zamkniętych polilinii na podanych warstwach zniszczeń!")
                    st.stop()

                zniszczenia_gdf_oryginalne = gpd.GeoDataFrame(geometry=zniszczenia_geoms, crs=f"EPSG:{epsg_code}")
                zniszczenia_gdf_1992 = zniszczenia_gdf_oryginalne.to_crs(epsg=2180)

                # Próbkowanie i zapytania do API
                all_points_with_meta = []
                for idx, poly in enumerate(zniszczenia_gdf_1992.geometry.tolist()):
                    pts = get_sampled_points(poly, distance=5.0)
                    for pt in pts:
                        all_points_with_meta.append((idx + 1, pt))

                obwiednie_id_dzialki = {i+1: set() for i in range(len(zniszczenia_gdf_1992))}
                
                def check_point(p_idx, pt):
                    r_id = zapytaj_uldk_xy(pt.x, pt.y)
                    return p_idx, r_id

                with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                    futures = [executor.submit(check_point, p_idx, pt) for p_idx, pt in all_points_with_meta]
                    for future in concurrent.futures.as_completed(futures):
                        p_idx, r_id = future.result()
                        if r_id:
                            obwiednie_id_dzialki[p_idx].add(r_id)

                wszystkie_unikalne_id = set()
                for ids in obwiednie_id_dzialki.values():
                    wszystkie_unikalne_id.update(ids)

                if not wszystkie_unikalne_id:
                    st.error("Skan ukończony, ale GUGiK nie odnalazł działek w tych miejscach.")
                    st.stop()

                dane_dzialek = []
                cache_dzialek = {}
                for id_dz in wszystkie_unikalne_id:
                    dane = pobierz_dane_dzialki_po_id(id_dz, epsg_code)
                    if dane:
                        dane_dzialek.append(dane)

                dzialki_df = pd.DataFrame(dane_dzialek).drop_duplicates(subset=['id_dzialki'])
                dzialki_gdf = gpd.GeoDataFrame(dzialki_df, geometry='geometry', crs=f"EPSG:{epsg_code}")

                dzialki_gdf['pow_dzialki_m2'] = dzialki_gdf.geometry.area
                intersekcja = gpd.overlay(dzialki_gdf, zniszczenia_gdf_oryginalne, how='intersection')
                intersekcja['pow_zniszczenia_m2'] = intersekcja.geometry.area

                wyniki = intersekcja.groupby(['id_dzialki', 'wojewodztwo', 'powiat', 'obreb', 'pow_dzialki_m2'])['pow_zniszczenia_m2'].sum().reset_index()

                # Formatowanie tekstów do DXF
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

                # Tworzenie nowego DXF
                out_doc = ezdxf.new('R2010')
                out_msp = out_doc.modelspace()
                out_doc.layers.add(name='DZIALKI', color=3)
                out_doc.layers.add(name='ZNISZCZENIA_WYNIK', color=1)
                out_doc.layers.add(name='OPISY', color=7)
                out_doc.layers.add(name='KABEL', color=5)

                for entity in msp.query(f'*[layer=="{kabel_layer}"]'):
                    out_msp.add_entity(entity.copy())

                for geom in dzialki_gdf.geometry:
                    if isinstance(geom, Polygon):
                        out_msp.add_lwpolyline(list(geom.exterior.coords), dxfattribs={'layer': 'DZIALKI'})

                for idx, row in intersekcja.iterrows():
                    if isinstance(row.geometry, Polygon):
                        out_msp.add_lwpolyline(list(row.geometry.exterior.coords), dxfattribs={'layer': 'ZNISZCZENIA_WYNIK', 'color': 1})
                        centroid = row.geometry.centroid
                        tekst = f"Dz: {fmt_dzialka(row['id_dzialki'])}\\nP: {fmt_pow(row['pow_zniszczenia_m2'])} m\\U+00B2"
                        out_msp.add_mtext(tekst, dxfattribs={'layer': 'OPISY', 'insert': (centroid.x, centroid.y), 'char_height': 0.5})

                bbox = zniszczenia_gdf_oryginalne.total_bounds
                tabela_x, tabela_y = bbox[2] + 20, bbox[3] 

                # Tabela 1
                out_msp.add_mtext("ZESTAWIENIE SZCZEGOLOWE ZNISZCZEN", dxfattribs={'insert': (tabela_x, tabela_y), 'char_height': 0.75, 'layer': 'OPISY'})
                y_offset = tabela_y - 1.5
                suma_szczegolowa = 0
                for i, row in intersekcja.iterrows():
                    suma_szczegolowa += row['pow_zniszczenia_m2']
                    linia_txt = f"Poligon {i+1} | Dz: {fmt_dzialka(row['id_dzialki'])} | Zniszcz: {fmt_pow(row['pow_zniszczenia_m2'])} m\\U+00B2"
                    out_msp.add_mtext(linia_txt, dxfattribs={'insert': (tabela_x, y_offset), 'char_height': 0.5, 'layer': 'OPISY'})
                    y_offset -= 1.0
                out_msp.add_mtext(f"SUMA CALKOWITA: {fmt_pow(suma_szczegolowa)} m\\U+00B2", dxfattribs={'insert': (tabela_x, y_offset - 0.5), 'char_height': 0.5, 'layer': 'OPISY', 'color': 1})

                # Tabela 2
                tabela2_x = tabela_x + 60
                out_msp.add_mtext("PODSUMOWANIE DLA DZIALEK", dxfattribs={'insert': (tabela2_x, tabela_y), 'char_height': 0.75, 'layer': 'OPISY'})
                y_offset2 = tabela_y - 1.5
                suma_zbiorcza = 0
                for i, row in wyniki.iterrows():
                    suma_zbiorcza += row['pow_zniszczenia_m2']
                    linia_txt = f"Dz: {fmt_dzialka(row['id_dzialki'])} | Lacznie zniszcz: {fmt_pow(row['pow_zniszczenia_m2'])} m\\U+00B2"
                    out_msp.add_mtext(linia_txt, dxfattribs={'insert': (tabela2_x, y_offset2), 'char_height': 0.5, 'layer': 'OPISY'})
                    y_offset2 -= 1.0
                out_msp.add_mtext(f"SUMA CALKOWITA: {fmt_pow(suma_zbiorcza)} m\\U+00B2", dxfattribs={'insert': (tabela2_x, y_offset2 - 0.5), 'char_height': 0.5, 'layer': 'OPISY', 'color': 1})

                # Zapis do pamięci jako bajty do pobrania
                dxf_bytes = io.BytesIO()
                out_doc.write(dxf_bytes)
                dxf_bytes.seek(0)

                excel_bytes = io.BytesIO()
                with pd.ExcelWriter(excel_bytes, engine='openpyxl') as writer:
                    intersekcja_excel = intersekcja.drop(columns=['geometry'], errors='ignore')
                    intersekcja_excel.to_excel(writer, sheet_name='Szczegółowe', index=False)
                    wyniki.to_excel(writer, sheet_name='Podsumowanie', index=False)
                excel_bytes.seek(0)

                st.success("Analiza zakończona sukcesem!")
                
                # Przyciski do pobrania plików
                col1, col2 = st.columns(2)
                with col1:
                    st.download_button("📥 Pobierz wynikowy DXF", data=dxf_bytes, file_name="Wynik_Geodezja.dxf", mime="application/dxf")
                with col2:
                    st.download_button("📊 Pobierz zestawienie Excel", data=excel_bytes, file_name="Zestawienie_Zniszczen.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            except Exception as e:
                st.error(f"Wystąpił błąd podczas przetwarzania: {e}")
