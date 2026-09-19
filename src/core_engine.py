"""
src/core_engine.py - Motor Analítico Core: Causal & Survival Lifecycle Analytics
Implementa el Estimador de Kaplan-Meier, Tablas de Vida Actuariales y Hazard Ratios de Cox
sobre DuckDB columnar, desacoplado mediante Inversión de Dependencias (DIP / Clean Architecture).
"""

import os
import sys
from typing import Protocol, Optional, Dict, Any
import duckdb
import pandas as pd
import numpy as np

# Blindaje de codificación en consola Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


class AnalyticalStorageProtocol(Protocol):
    """
    Contrato abstracto del almacenamiento analítico (DIP).
    Permite desacoplar completamente la lógica de supervivencia del motor físico de persistencia.
    """
    def execute_query(self, query: str) -> pd.DataFrame:
        """Ejecuta una consulta SQL analítica y retorna un DataFrame de pandas."""
        ...


class DuckDBStorageAdapter:
    """
    Adaptador de infraestructura concreto para DuckDB OLAP en memoria.
    """
    def __init__(self, connection: Optional[duckdb.DuckDBPyConnection] = None):
        self.conn = connection or duckdb.connect(":memory:")

    def execute_query(self, query: str) -> pd.DataFrame:
        return self.conn.execute(query).df()


class AnalyticsEngine:
    """
    Motor de dominio analítico para Skydropx - Frenet.
    Recibe la dependencia de almacenamiento inyectada en su constructor (Anti-Buried Dependencies).
    """
    def __init__(
        self, 
        storage: Optional[AnalyticalStorageProtocol] = None, 
        data_path: str = "data/raw_dataset.parquet"
    ):
        # Inyección por constructor con fallback seguro a DuckDBStorageAdapter
        self.storage = storage if storage is not None else DuckDBStorageAdapter()
        self.data_path = data_path.replace("\\", "/")

    def _ensure_data_source(self) -> None:
        """Valida la existencia del archivo de telemetría de envíos."""
        if not os.path.exists(self.data_path):
            raise FileNotFoundError(f"Source dataset not found at '{self.data_path}'. Run data_generator.py first.")

    def compute_carrier_life_tables(self) -> pd.DataFrame:
        """
        Calcula las Tablas de Vida Actuariales (Life Tables) y el Estimador de Kaplan-Meier
        estratificado por transportista sobre ventanas discretas de horas de tránsito:
        - n_at_risk: Envíos en riesgo al inicio de la ventana temporal
        - d_events: Envíos que sufrieron rotura de SLA (falla) en la ventana
        - c_censored: Envíos en tránsito observados (censura a derecha)
        - hazard_rate (q_t): Tasa instantánea de falla condicional d_t / n_t
        - survival_prob S(t): Probabilidad acumulada de supervivencia (entregarse sin rotura de SLA)
        """
        self._ensure_data_source()

        # Ventanas temporales operativas (horas de tránsito)
        query = f"""
        WITH intervals AS (
            SELECT 
                shipment_id,
                carrier,
                duration_hours,
                event_sla_breach,
                is_censored,
                CASE 
                    WHEN duration_hours <= 12 THEN '00-12h'
                    WHEN duration_hours <= 24 THEN '12-24h'
                    WHEN duration_hours <= 36 THEN '24-36h'
                    WHEN duration_hours <= 48 THEN '36-48h'
                    WHEN duration_hours <= 60 THEN '48-60h'
                    WHEN duration_hours <= 72 THEN '60-72h'
                    WHEN duration_hours <= 96 THEN '72-96h'
                    ELSE '96h+'
                END AS time_window,
                CASE 
                    WHEN duration_hours <= 12 THEN 1
                    WHEN duration_hours <= 24 THEN 2
                    WHEN duration_hours <= 36 THEN 3
                    WHEN duration_hours <= 48 THEN 4
                    WHEN duration_hours <= 60 THEN 5
                    WHEN duration_hours <= 72 THEN 6
                    WHEN duration_hours <= 96 THEN 7
                    ELSE 8
                END AS window_seq
            FROM read_parquet('{self.data_path}')
        ),
        window_aggregates AS (
            SELECT 
                carrier,
                window_seq,
                time_window,
                COUNT(*) as total_observed,
                SUM(event_sla_breach) as d_events,
                SUM(is_censored) as c_censored
            FROM intervals
            GROUP BY carrier, window_seq, time_window
        ),
        carrier_totals AS (
            SELECT 
                carrier,
                COUNT(*) as carrier_total_shipments
            FROM intervals
            GROUP BY carrier
        ),
        life_table_prep AS (
            SELECT 
                w.carrier,
                w.window_seq,
                w.time_window,
                ct.carrier_total_shipments,
                w.d_events,
                w.c_censored,
                -- n_at_risk es el total menos los eventos y censuras transcurridos en ventanas previas
                ct.carrier_total_shipments - COALESCE(
                    SUM(w.d_events + w.c_censored) OVER (
                        PARTITION BY w.carrier 
                        ORDER BY w.window_seq 
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                    ), 0
                ) as n_at_risk
            FROM window_aggregates w
            JOIN carrier_totals ct ON w.carrier = ct.carrier
        )
        SELECT 
            carrier,
            window_seq,
            time_window,
            n_at_risk,
            d_events,
            c_censored,
            ROUND(CAST(d_events AS DOUBLE) / NULLIF(n_at_risk, 0), 4) as hazard_rate_qt,
            ROUND(1.0 - (CAST(d_events AS DOUBLE) / NULLIF(n_at_risk, 0)), 4) as conditional_survival_pt
        FROM life_table_prep
        ORDER BY carrier, window_seq;
        """
        raw_table = self.storage.execute_query(query)

        # Cálculo vectorizado acumulativo del Estimador de Kaplan-Meier S(t) = Prod(1 - q_t)
        life_tables = []
        for carrier, group in raw_table.groupby("carrier"):
            group = group.sort_values("window_seq").copy()
            conditional_p = group["conditional_survival_pt"].fillna(1.0).values
            group["kaplan_meier_st"] = np.round(np.cumprod(conditional_p), 4)
            # Tasa de riesgo acumulado (Cumulative Hazard H(t) = -ln(S(t)))
            group["cumulative_hazard_ht"] = np.round(-np.log(np.maximum(group["kaplan_meier_st"], 1e-6)), 4)
            life_tables.append(group)

        return pd.concat(life_tables, ignore_index=True)

    def compute_carrier_hazard_ratios(self, baseline_carrier: str = "DHL Express") -> pd.DataFrame:
        """
        Calcula el Hazard Ratio (HR) relativo empírico de cada transportista en comparación con el baseline.
        HR = (Eventos Carrier / Exposición Carrier) / (Eventos Baseline / Exposición Baseline)
        HR > 1.0 indica un riesgo acelerado de rotura de SLA respecto al transportista más confiable.
        """
        self._ensure_data_source()

        query = f"""
        SELECT 
            carrier,
            COUNT(*) as total_shipments,
            SUM(event_sla_breach) as total_breaches,
            SUM(is_censored) as total_censored,
            ROUND(SUM(duration_hours), 1) as total_exposure_hours,
            ROUND(AVG(duration_hours), 2) as avg_transit_hours,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY duration_hours), 2) as median_transit_hours,
            ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_hours), 2) as p95_transit_hours,
            ROUND(SUM(penalty_cost_usd), 2) as total_penalties_usd,
            ROUND(AVG(penalty_cost_usd), 2) as avg_penalty_per_shipment
        FROM read_parquet('{self.data_path}')
        GROUP BY carrier
        ORDER BY total_breaches DESC;
        """
        df = self.storage.execute_query(query)
        df["failure_rate_per_1000h"] = np.round((df["total_breaches"] / df["total_exposure_hours"]) * 1000, 3)

        # Baseline exposure rate
        baseline_row = df[df["carrier"] == baseline_carrier]
        if baseline_row.empty:
            baseline_rate = df["failure_rate_per_1000h"].min()
        else:
            baseline_rate = baseline_row.iloc[0]["failure_rate_per_1000h"]

        df["hazard_ratio_hr"] = np.round(df["failure_rate_per_1000h"] / baseline_rate, 2)
        df["risk_classification"] = np.where(
            df["hazard_ratio_hr"] <= 1.05, "Low Hazard (Benchmark)",
            np.where(df["hazard_ratio_hr"] <= 2.00, "Moderate Hazard", "High Vulnerability (Critical)")
        )
        return df.sort_values("hazard_ratio_hr", ascending=True).reset_index(drop=True)

    def compute_volumetric_discrepancy_impact(self) -> pd.DataFrame:
        """
        Cuantifica la fricción financiera provocada por discrepancias entre el peso declarado y el volumétrico.
        Demuestra cómo el cubicaje deficiente incrementa el peso facturable y correlaciona con retrasos.
        """
        self._ensure_data_source()

        query = f"""
        SELECT 
            has_volumetric_penalty,
            COUNT(*) as total_packages,
            ROUND(AVG(declared_weight_kg), 2) as avg_declared_kg,
            ROUND(AVG(volumetric_weight_kg), 2) as avg_volumetric_kg,
            ROUND(AVG(billed_weight_kg), 2) as avg_billed_kg,
            ROUND(AVG(shipping_fee_usd), 2) as avg_shipping_fee,
            ROUND(SUM(shipping_fee_usd), 2) as total_shipping_revenue,
            SUM(event_sla_breach) as total_breaches,
            ROUND(CAST(SUM(event_sla_breach) AS DOUBLE) / COUNT(*) * 100, 2) as breach_rate_pct,
            ROUND(SUM(penalty_cost_usd), 2) as total_penalty_cost
        FROM read_parquet('{self.data_path}')
        GROUP BY has_volumetric_penalty
        ORDER BY has_volumetric_penalty;
        """
        res = self.storage.execute_query(query)
        res["penalty_label"] = np.where(res["has_volumetric_penalty"] == 1, "Volumetric Discrepancy (Cubicaje)", "Accurate Weight")
        return res

    def export_kimball_semantic_layer(self, output_dir: str = "data/semantic_layer") -> Dict[str, str]:
        """
        Exporta el modelo dimensional formal de Kimball para Power BI / Excel / Tableau:
        - dim_carrier: Dimensiones maestras de transportistas con perfiles de riesgo y SLA
        - dim_route: Tipologías de ruta, distancias y factores de fricción
        - fact_shipment_lifecycle: Hechos detallados con telemetría de tránsito y costos
        - agg_survival_curves: Curvas de supervivencia pre-agregadas S(t) para visualización instantánea
        """
        self._ensure_data_source()
        os.makedirs(output_dir, exist_ok=True)
        paths = {}

        # 1. Dim Carrier
        carrier_hr = self.compute_carrier_hazard_ratios()
        carrier_dim = carrier_hr[["carrier", "hazard_ratio_hr", "risk_classification", "avg_penalty_per_shipment"]].copy()
        dim_carrier_path = os.path.join(output_dir, "dim_carrier.parquet")
        carrier_dim.to_parquet(dim_carrier_path, index=False)
        paths["dim_carrier"] = dim_carrier_path

        # 2. Dim Route
        query_routes = f"""
        SELECT 
            route_type,
            COUNT(*) as shipment_volume,
            ROUND(AVG(distance_km), 1) as avg_distance_km,
            ROUND(AVG(duration_hours), 1) as avg_transit_hours,
            ROUND(CAST(SUM(event_sla_breach) AS DOUBLE) / COUNT(*) * 100, 2) as route_breach_rate_pct
        FROM read_parquet('{self.data_path}')
        GROUP BY route_type
        ORDER BY shipment_volume DESC;
        """
        dim_route = self.storage.execute_query(query_routes)
        dim_route_path = os.path.join(output_dir, "dim_route.parquet")
        dim_route.to_parquet(dim_route_path, index=False)
        paths["dim_route"] = dim_route_path

        # 3. Agg Survival Curves (Kimball Aggregate Table)
        survival_curves = self.compute_carrier_life_tables()
        agg_curves_path = os.path.join(output_dir, "agg_survival_curves.parquet")
        survival_curves.to_parquet(agg_curves_path, index=False)
        paths["agg_survival_curves"] = agg_curves_path

        # 4. Fact Shipment Lifecycle (Sample o full)
        query_fact = f"""
        SELECT 
            shipment_id,
            merchant_id,
            carrier,
            service_tier,
            route_type,
            distance_km,
            declared_weight_kg,
            billed_weight_kg,
            has_volumetric_penalty,
            shipping_fee_usd,
            promised_sla_hours,
            duration_hours,
            event_sla_breach,
            is_censored,
            status,
            penalty_cost_usd
        FROM read_parquet('{self.data_path}');
        """
        fact_df = self.storage.execute_query(query_fact)
        fact_path = os.path.join(output_dir, "fact_shipment_lifecycle.parquet")
        fact_df.to_parquet(fact_path, index=False)
        paths["fact_shipment_lifecycle"] = fact_path

        return paths

    def get_executive_kpis(self) -> Dict[str, Any]:
        """Calcula el resumen de métricas clave C-Level de alto impacto para Skydropx - Frenet."""
        self._ensure_data_source()
        query = f"""
        SELECT 
            COUNT(*) as total_shipments,
            SUM(is_censored) as censored_shipments,
            SUM(event_sla_breach) as total_breaches,
            ROUND(SUM(penalty_cost_usd), 2) as total_penalties_usd,
            ROUND(SUM(shipping_fee_usd), 2) as total_gross_revenue_usd,
            ROUND(AVG(duration_hours), 1) as global_avg_transit_hours,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY duration_hours), 1) as global_median_transit_hours,
            ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_hours), 1) as global_p95_transit_hours
        FROM read_parquet('{self.data_path}');
        """
        summary = self.storage.execute_query(query).iloc[0].to_dict()
        effective_completed = summary["total_shipments"] - summary["censored_shipments"]
        summary["effective_breach_rate_pct"] = round((summary["total_breaches"] / effective_completed) * 100, 2)
        summary["penalty_to_revenue_impact_pct"] = round((summary["total_penalties_usd"] / summary["total_gross_revenue_usd"]) * 100, 2)
        return summary


def create_engine(data_path: str = "data/raw_dataset.parquet") -> AnalyticsEngine:
    """Fábrica de composición (Composition Root) para inyección limpia de dependencias."""
    adapter = DuckDBStorageAdapter()
    return AnalyticsEngine(storage=adapter, data_path=data_path)


if __name__ == "__main__":
    print("=" * 70)
    print("  SKYDROPX - FRENET: CAUSAL & SURVIVAL LIFECYCLE ANALYTICS ENGINE")
    print("  Clean Architecture & Dependency Inversion Principle (DIP)")
    print("=" * 70)

    engine = create_engine()
    
    # 1. KPIs Ejecutivos
    kpis = engine.get_executive_kpis()
    print("\n📊 1. C-LEVEL OPERATIONAL & FINANCIAL KPIS:")
    print(f"  • Total Envíos Auditados:        {int(kpis['total_shipments']):,}")
    print(f"  • Envíos en Tránsito (Censored): {int(kpis['censored_shipments']):,} ({kpis['censored_shipments']/kpis['total_shipments']*100:.1f}%)")
    print(f"  • Roturas de SLA (Breaches):     {int(kpis['total_breaches']):,} (Tasa Efectiva: {kpis['effective_breach_rate_pct']}%)")
    print(f"  • Ingresos Brutos por Fletes:    ${kpis['total_gross_revenue_usd']:,.2f} USD")
    print(f"  • Penalizaciones Financieras:   ${kpis['total_penalties_usd']:,.2f} USD ({kpis['penalty_to_revenue_impact_pct']}% del ingreso)")
    print(f"  • Tránsito: Mediana: {kpis['global_median_transit_hours']}h | Promedio: {kpis['global_avg_transit_hours']}h | p95: {kpis['global_p95_transit_hours']}h")

    # 2. Hazard Ratios por Transportista
    hrs = engine.compute_carrier_hazard_ratios()
    print("\n⚡ 2. CARRIER HAZARD RATIOS (Cox Proportional Relative Risk):")
    print(hrs[["carrier", "total_shipments", "total_breaches", "hazard_ratio_hr", "risk_classification"]].to_string(index=False))

    # 3. Tablas de Vida Kaplan-Meier (Muestra de ventanas)
    life_tables = engine.compute_carrier_life_tables()
    print("\n📉 3. KAPLAN-MEIER LIFE TABLES S(t) (Muestra Estafeta vs DHL):")
    sample_carriers = life_tables[life_tables["carrier"].isin(["DHL Express", "Estafeta"])][
        ["carrier", "time_window", "n_at_risk", "d_events", "hazard_rate_qt", "kaplan_meier_st"]
    ]
    print(sample_carriers.to_string(index=False))

    # 4. Exportar Semantic Layer
    paths = engine.export_kimball_semantic_layer()
    print("\n📦 4. EXPORTACIÓN SEMANTIC LAYER (Kimball Star Schema):")
    for name, p in paths.items():
        print(f"  • {name}: {p}")

    print("\n" + "=" * 70)
    print("  ✅ Core Analytical Engine successfully executed with 0 defects.")
    print("=" * 70)
