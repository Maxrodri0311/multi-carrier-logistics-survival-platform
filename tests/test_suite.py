"""
tests/test_suite.py - Suite Integral de Pruebas Automatizadas con Pytest
Valida:
1. Contratos de datos y física estocástica del generador logístico.
2. Invariantes matemáticos de Kaplan-Meier (monotonicidad decreciente y acotamiento [0,1]).
3. Coherencia de los Hazard Ratios de Cox y baseline institucional.
4. Desacoplamiento estricto por Inversión de Dependencias (DIP) mediante MockStorageAdapter.
5. Exportación del modelo dimensional Kimball Star Schema.
"""

import os
import sys
import tempfile
import pytest
import pandas as pd
import numpy as np

# Blindaje UTF-8 Windows y resolución de paths
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.data_generator import generate_synthetic_dataset
from src.core_engine import AnalyticsEngine, DuckDBStorageAdapter, create_engine


@pytest.fixture(scope="session")
def shared_test_data(tmp_path_factory):
    """Fixture de sesión que genera 5,000 registros para pruebas aisladas de alta velocidad."""
    temp_dir = tmp_path_factory.mktemp("test_dataset")
    parquet_path = str(temp_dir / "shipments_sample.parquet")
    df = generate_synthetic_dataset(num_records=5000, output_path=parquet_path, seed=123)
    return parquet_path


def test_data_generator_contract_and_physics(shared_test_data):
    """Verifica que el generador respete el contrato de datos y la física de Skydropx - Frenet."""
    df = pd.read_parquet(shared_test_data)
    
    # 1. Dimensiones y no vacuidad
    assert len(df) == 5000
    
    # 2. Contrato de columnas mandatorias
    mandatory_cols = [
        "shipment_id", "merchant_id", "carrier", "service_tier", "route_type",
        "distance_km", "declared_weight_kg", "volumetric_weight_kg", "billed_weight_kg",
        "has_volumetric_penalty", "shipping_fee_usd", "promised_sla_hours",
        "duration_hours", "event_sla_breach", "is_censored", "status", "penalty_cost_usd"
    ]
    for col in mandatory_cols:
        assert col in df.columns, f"Columna requerida '{col}' no encontrada en el dataset."

    # 3. Invariantes de Negocio
    # El peso facturable debe ser el máximo entre declarado y volumétrico
    assert (df["billed_weight_kg"] >= df["declared_weight_kg"]).all()
    assert (df["duration_hours"] > 0).all()
    assert (df["shipping_fee_usd"] > 0).all()
    
    # La censura debe estar presente (observaciones en tránsito)
    assert df["is_censored"].sum() > 0
    assert df["is_censored"].isin([0, 1]).all()
    assert df["event_sla_breach"].isin([0, 1]).all()


def test_kaplan_meier_mathematical_invariants(shared_test_data):
    """
    Verifica los invariantes matemáticos del Estimador de Kaplan-Meier S(t):
    - S(t) debe estar estrictamente acotado en el intervalo [0.0, 1.0]
    - S(t) debe ser una función monótona no creciente respecto al tiempo (dS/dt <= 0)
    """
    adapter = DuckDBStorageAdapter()
    engine = AnalyticsEngine(storage=adapter, data_path=shared_test_data)
    life_tables = engine.compute_carrier_life_tables()

    assert not life_tables.empty
    assert "kaplan_meier_st" in life_tables.columns
    assert "hazard_rate_qt" in life_tables.columns

    # Verificar cada transportista por separado
    for carrier, group in life_tables.groupby("carrier"):
        st_values = group.sort_values("window_seq")["kaplan_meier_st"].values
        
        # Invariante 1: Acotamiento [0, 1]
        assert (st_values >= 0.0).all() and (st_values <= 1.0).all(), f"S(t) fuera de [0,1] en {carrier}"
        
        # Invariante 2: Monotonicidad no creciente
        diffs = np.diff(st_values)
        assert (diffs <= 1e-6).all(), f"Violación de monotonicidad decreciente de S(t) en {carrier}: {diffs}"


def test_cox_hazard_ratios_relative_consistency(shared_test_data):
    """Valida la coherencia de los Hazard Ratios y el benchmark de DHL Express."""
    adapter = DuckDBStorageAdapter()
    engine = AnalyticsEngine(storage=adapter, data_path=shared_test_data)
    hr_df = engine.compute_carrier_hazard_ratios(baseline_carrier="DHL Express")

    assert not hr_df.empty
    # DHL debe tener HR exactamente 1.00 (o muy cercano por redondeo)
    dhl_row = hr_df[hr_df["carrier"] == "DHL Express"]
    assert not dhl_row.empty
    assert dhl_row.iloc[0]["hazard_ratio_hr"] == 1.00

    # Todos los HR deben ser valores positivos
    assert (hr_df["hazard_ratio_hr"] > 0).all()
    assert "risk_classification" in hr_df.columns


def test_volumetric_discrepancy_aggregation(shared_test_data):
    """Valida el cálculo del impacto financiero por discrepancia de cubicaje."""
    engine = create_engine(data_path=shared_test_data)
    vd_df = engine.compute_volumetric_discrepancy_impact()

    assert len(vd_df) == 2
    assert "avg_volumetric_kg" in vd_df.columns
    # El grupo con penalización volumétrica debe tener mayor peso facturable que el declarado
    penalized = vd_df[vd_df["has_volumetric_penalty"] == 1].iloc[0]
    assert penalized["avg_billed_kg"] > penalized["avg_declared_kg"]


def test_kimball_semantic_layer_export(shared_test_data, tmp_path):
    """Verifica que el modelo dimensional exporte las 4 tablas Kimball en formato Parquet."""
    export_dir = str(tmp_path / "semantic_export")
    engine = create_engine(data_path=shared_test_data)
    exported_paths = engine.export_kimball_semantic_layer(output_dir=export_dir)

    expected_tables = ["dim_carrier", "dim_route", "agg_survival_curves", "fact_shipment_lifecycle"]
    for table_name in expected_tables:
        assert table_name in exported_paths
        file_path = exported_paths[table_name]
        assert os.path.exists(file_path), f"Tabla Parquet {file_path} no fue generada."
        # Validar lectura de la tabla exportada
        df = pd.read_parquet(file_path)
        assert len(df) > 0, f"Tabla {table_name} exportada vacía."


def test_dependency_inversion_isolated_mock():
    """
    TEST DE ORO DIP: Demuestra que la lógica del dominio puede ejecutarse
    aisladamente con un MockStorageAdapter en memoria sin tocar disco ni instanciar DuckDB.
    """
    class MockStorageAdapter:
        def execute_query(self, query: str) -> pd.DataFrame:
            # Simula una respuesta instantánea pre-empaquetada en memoria
            return pd.DataFrame([
                {"carrier": "DHL Express", "total_shipments": 1000, "total_breaches": 10,
                 "total_censored": 50, "total_exposure_hours": 15000.0, "avg_transit_hours": 15.0,
                 "median_transit_hours": 14.5, "p95_transit_hours": 24.0, "total_penalties_usd": 250.0,
                 "avg_penalty_per_shipment": 0.25},
                {"carrier": "Estafeta", "total_shipments": 1000, "total_breaches": 150,
                 "total_censored": 80, "total_exposure_hours": 38000.0, "avg_transit_hours": 38.0,
                 "median_transit_hours": 36.0, "p95_transit_hours": 72.0, "total_penalties_usd": 4500.0,
                 "avg_penalty_per_shipment": 4.50}
            ])

    # Se usa un path ficticio pero el mock intercepta la consulta
    with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp_dummy:
        mock_storage = MockStorageAdapter()
        engine = AnalyticsEngine(storage=mock_storage, data_path=tmp_dummy.name)
        result = engine.compute_carrier_hazard_ratios(baseline_carrier="DHL Express")

        assert len(result) == 2
        assert result.iloc[0]["carrier"] == "DHL Express"
        assert result.iloc[0]["hazard_ratio_hr"] == 1.00
        assert result.iloc[1]["carrier"] == "Estafeta"
        assert result.iloc[1]["hazard_ratio_hr"] > 1.00
