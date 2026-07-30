# Plan — lector de SEC Form 4 (insiders) y 13F (superinvestors)

**Objetivo:** cubrir dos requisitos del reporte sin depender de FMP, leyendo directo de SEC EDGAR,
que los publica gratis y sin límite.

**Estado: construido el 2026-07-20.** Vive en `engine/wbj/filings/` y entra al packet como
`insiders_edgar` y `superinvestors`. 54 tests, todos los fixtures son filings reales. Lo único
que sigue pendiente es de decisión, no de código: **cuánto peso tiene esto en el puntaje**.

---

## Por qué EDGAR y no FMP

FMP vende estos datos pero los 402 en el plan actual, y además solo cubre una lista fija de
tickers. EDGAR los da para **cualquier** empresa de EE.UU., gratis y para siempre. FMP no es dueño
del dato: lo toma de EDGAR, lo empaqueta y lo cobra.

Verificado: NVDA tiene **566 Form 4** disponibles ahora mismo.

> ⚠️ **Corregido al construirlo (2026-07-20).** Este párrafo decía antes que NVDA también tenía
> "10 13F-HR disponibles", dando a entender que servían para saber quién posee NVDA. **No sirven.**
> Esos diez son el portafolio *propio* de NVIDIA — sus posiciones en Intel ($9.5B), CoreWeave y
> Synopsys. NVDA no aparece en su propia tabla. Ver "Cómo se llega al dato" abajo.

---

## Cómo se llega al dato

**Los dos formularios se leen en direcciones opuestas.** Esto es lo más importante de esta
sección y es lo que el plan original tenía mal.

### Form 4 — se entra por el CIK de la empresa

El insider declara **bajo el CIK del emisor**, así que preguntar por NVDA devuelve directamente a
sus insiders. Es una consulta.

1. `https://data.sec.gov/submissions/CIK{cik:010d}.json` lista todos los filings recientes con su
   `form`, `filingDate`, `accessionNumber` y `primaryDocument`.
2. Filtrar `form == "4"` — exacto, no por prefijo: `"4/A"` es una enmienda que reformula una
   operación ya contada.
3. `primaryDocument` trae el prefijo `xslF345X06/`, que es la vista HTML. Quitándolo sale el XML
   crudo en el mismo directorio, sin pedir nada extra.
4. El XML vive en
   `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_sin_guiones}/{archivo}.xml`.

### 13F — se entra por el CIK del fondo, uno por uno

**No existe una consulta a EDGAR de "quién posee X".** El 13F lo declara el fondo bajo *su propio*
CIK, no bajo el de la empresa poseída. Entrar por el CIK del emisor devuelve lo que ese emisor
invierte, que es otra pregunta.

La única vía es **escanear**: leer la tabla de cada fondo de una lista curada y buscar el CUSIP de
la empresa adentro. Eso es exactamente lo que venden los agregadores comerciales.

1. Mantener una lista de CIKs de gestores que valga la pena leer (`TRACKED_FUNDS_META`).
2. Por cada fondo, sus dos 13F-HR más recientes: el actual y el trimestre con el que comparar.
3. El nombre del archivo de la tabla **no es predecible** — `primaryDocument` apunta a
   `primary_doc.xml`, que es la carátula, no las posiciones. Hay que leer `index.json` de la
   accession y tomar el `.xml` que no sea la carátula. En Berkshire se llama `53405.xml`; en
   NVIDIA, `information_table.xml`.
4. Buscar por **CUSIP, nunca por nombre** — los fondos escriben "NVIDIA CORP" o "NVIDIA
   CORPORATION" y EDGAR dice "NVIDIA Corp". El nombre sirve solo para *descubrir* el CUSIP una vez.

Una empresa ausente del resultado está ausente **de esa lista**, que es una afirmación mucho más
débil que "ningún inversionista importante la tiene". El reporte lo dice así.

### Para ambos

La SEC exige cabecera `User-Agent` identificable (política de fair access). `Provider.get_json`
ya acepta `headers`; `EdgarProvider` es el lugar donde extender.

---

## La parte difícil: los códigos de transacción

**Bajar el archivo es trivial. Interpretarlo no.**

Ejemplo real (NVDA, accession `0001197647-26-000005`): el director Tench Coxe movió **500,000
acciones a $0.00** bajo código **`G`** — un *regalo*, probablemente a un fideicomiso familiar.

Un lector ingenuo reporta *"director se deshizo de 500,000 acciones"* e insinúa que está huyendo.
Es falso. Hay ~20 códigos y cada uno significa algo distinto. Los que más importan:

| Código | Qué es | Cómo tratarlo |
|---|---|---|
| `P` | Compra en mercado abierto | **La señal fuerte.** Se compra por una sola razón |
| `S` | Venta en mercado abierto | Señal débil — mil razones posibles |
| `M` | Ejercicio de opciones | **No es compra.** Confundirlo es el error clásico |
| `A` | Concesión / premio | Compensación, no convicción |
| `G` | Regalo / transferencia | Ni compra ni venta |
| `F` | Retención para impuestos | Ruido |

Las ventas bajo plan **10b5-1** se programan con meses de anticipación: hay un campo que lo
indica y no deben leerse como señal.

---

## Las otras trampas (encontradas al construirlo, 2026-07-20)

Los códigos de transacción eran la dificultad anticipada y se comportaron como el plan decía.
Estas tres no estaban previstas y ninguna es visible leyendo la documentación — las tres
aparecieron corriendo contra data real:

**1. Un 13F parte una posición en varias filas, una por gestora.** La posición de Apple de
Berkshire son **12 filas** (GEICO, National Indemnity, etc.). La primera dice 692,000 acciones;
la real es 227,917,808. Tomar la primera fila subestima 329x, y con un número perfectamente
creíble que nada aguas abajo va a marcar. Hay que **sumar por CUSIP**. En Berkshire, 18 de 29
CUSIPs vienen partidos. Validación: Coca-Cola debe dar exactamente 400,000,000 acciones.

**2. Algunos fondos todavía declaran `value` en miles.** La SEC pasó a dólares enteros en 2023 y
no todos migraron. Baupost reporta Alphabet como 1,181,131 acciones por 338,819 — leído en dólares
son $0.29 por acción. Se detecta por el **precio implícito mediano del filing completo**
(`value/shares` < $1), nunca fila por fila: una acción de centavos es posible, un fondo entero
cotizando bajo un dólar no.

**3. Un CIK equivocado falla en silencio y de la peor forma.** El CIK 1637460 se lee perfectamente
como Scion de Michael Burry — es **Man Group plc**. Estuvo a punto de quedar así, y el reporte
habría atribuido las posiciones de un gestor a otro. Los correctos: Scion `1649339`, Duquesne
Family Office `1536411` (el `1008925` es el Duquesne viejo, cerrado en 2010). Cada CIK guarda
también el nombre que EDGAR devuelve, y `scripts/verify_fund_ciks.py` los re-verifica.

**Los gestores indexados quedan fuera a propósito.** BlackRock y Vanguard tienen casi todas las
empresas de EE.UU.; incluirlos pondría un poseedor en todos los tickers y la sección diría siempre
lo mismo.

---

## Reglas que ya existen

Están en `Saiyan AI/.claude/agents/business-analysis.md` (sección "Quién la posee"):

- **Umbral $1M.** Por debajo es ruido y no entra al reporte.
- **Compra ≠ venta en significado.** Ponderar en consecuencia y decirlo.
- Distinguir compra abierta de ejercicio de opciones.
- Ojo con los 10b5-1.
- Para cada operación: quién, cargo, monto, fecha, compra vs venta.
- **13F:** quién, tamaño, si subió o bajó, y qué otras empresas exitosas ha tenido ese
  inversionista. Reportar la **fecha del filing** — llegan con hasta 45 días de retraso y solo
  muestran posiciones largas en acciones de EE.UU.

**Falta definir (decisión de Melvin, no programación):** cuánto peso tiene esto en el puntaje.

---

## Orden sugerido

1. `EdgarProvider.filings_of_type(cik, "4")` — listar accessions. Test con fixture.
2. Localizar y bajar el XML del filing.
3. Parser de Form 4: emisor, persona, cargo, fecha, código, acciones, precio, adquirido/dispuesto.
   **Aquí van la mayoría de los tests** — un caso por código relevante.
4. Filtro >$1M y agregación por persona.
5. **13F-HR — NO es "lo mismo" que los pasos 1-4.** Cambia la dirección de la búsqueda (ver
   "Cómo se llega al dato"), el formato (`informationTable`, con namespace XML, que el Form 4 no
   tiene), y el archivo hay que localizarlo vía `index.json`. Es un escaneo sobre una lista curada
   de fondos, no una consulta por ticker.
6. Conectar al packet y al reporte.

Construir por partes y mostrar cada una funcionando; no dejarlo como caja negra.

---

## Límites que hay que decir en el reporte

- Los 13F llegan con hasta **45 días de retraso**. Nunca presentarlos como posición de hoy.
- Solo posiciones **largas** en acciones de EE.UU. Las apuestas en contra no aparecen.
- El dato es un **hecho**, no una recomendación: "el CFO vendió $3M en marzo". El juicio es del
  inversionista.
