"""
src/data_generator.py - Generador de Datos Sintéticos con Física Estocástica Real
Modela la dinámica logística multi-carrier de Skydropx - Frenet (50,000+ registros).
Incorpora censura a derecha, colas pesadas de tránsito, discrepancia de cubicaje y fallas de SLA.
"""

import os
import sys
import time
import argparse
import numpy as np
import pandas as pd

# Blindaje de consola Windows UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def generate_synthetic_dataset(
    num_records: int = 50000, 
    output_path: str = "data/raw_dataset.parquet",
    seed: int = 42
) -> pd.DataFrame:
    """
    Genera un dataset logístico masivo con física estocástica realista para análisis de supervivencia.
    """
    print(f"[Data Generator] Generating {num_records:,} logistics shipments for Skydropx - Frenet...")
    start_time = time.perf_counter()
    np.random.seed(seed)

    # 1. Identificadores únicos
    shipment_ids = [f"SHP-{i:07d}" for i in range(100001, 100001 + num_records)]
    merchant_ids = [f"MERCH-{i:04d}" for i in np.random.randint(1, 450, size=num_records)]

    # 2. Transportistas y Cuotas de Mercado reales en pasarelas logísticas
    carriers = ["FedEx", "DHL Express", "Estafeta", "99Minutos", "Redpack"]
    carrier_probs = [0.28, 0.22, 0.25, 0.15, 0.10]
    assigned_carriers = np.random.choice(carriers, size=num_records, p=carrier_probs)

    # 3. Tipos de Servicio y SLA prometido (horas)
    service_tiers = ["Express_NextDay", "Standard_Ground", "Economy_Parcel"]
    tier_probs = [0.25, 0.55, 0.20]
    assigned_tiers = np.random.choice(service_tiers, size=num_records, p=tier_probs)

    sla_mapping = {
        "Express_NextDay": 24.0,
        "Standard_Ground": 48.0,
        "Economy_Parcel": 72.0
    }
    promised_sla = np.array([sla_mapping[t] for t in assigned_tiers])

    # 4. Tipología de Ruta y Distancia (km)
    routes = ["Local_Metro", "Regional_Hub", "National_LongHaul"]
    route_probs = [0.35, 0.40, 0.25]
    assigned_routes = np.random.choice(routes, size=num_records, p=route_probs)

    distance_km = np.where(
        assigned_routes == "Local_Metro",
        np.random.uniform(5.0, 45.0, size=num_records),
        np.where(
            assigned_routes == "Regional_Hub",
            np.random.uniform(50.0, 350.0, size=num_records),
            np.random.uniform(400.0, 1800.0, size=num_records)
        )
    )

    # 5. Pesos y Discrepancias Volumétricas (Cubicaje)
    # 24% de los merchants sufren discrepancia por empaquetado deficiente
    declared_weight_kg = np.round(np.random.lognormal(mean=0.8, sigma=0.5, size=num_records), 2)
    has_volumetric_penalty = np.random.binomial(1, 0.24, size=num_records)
    
    volumetric_inflation = np.where(
        has_volumetric_penalty == 1,
        np.random.uniform(1.35, 2.80, size=num_records),
        np.random.uniform(0.90, 1.15, size=num_records)
    )
    volumetric_weight_kg = np.round(declared_weight_kg * volumetric_inflation, 2)
    billed_weight_kg = np.maximum(declared_weight_kg, volumetric_weight_kg)

    # 6. Tarifa de Flete y Penalizaciones
    base_carrier_rates = {
        "DHL Express": 7.5,
        "FedEx": 6.8,
        "Estafeta": 4.5,
        "99Minutos": 3.8,
        "Redpack": 3.2
    }
    rate_multiplier = np.array([base_carrier_rates[c] for c in assigned_carriers])
    shipping_fee_usd = np.round(rate_multiplier + (billed_weight_kg * 1.85) + (distance_km * 0.004), 2)

    # 7. Modelado Estocástico del Tiempo de Tránsito Real (Física de Supervivencia)
    # Definimos factores de aceleración/retraso por transportista (Hazard Multipliers)
    # DHL y FedEx tienen varianzas compactas; Estafeta y Redpack sufren colas pesadas de retraso
    carrier_mean_log = {
        "DHL Express": 2.70,   # ~15h median
        "FedEx": 2.95,         # ~19h median
        "99Minutos": 3.10,     # ~22h median
        "Estafeta": 3.65,      # ~38h median
        "Redpack": 3.85        # ~47h median
    }
    carrier_sigma_log = {
        "DHL Express": 0.35,
        "FedEx": 0.42,
        "99Minutos": 0.50,
        "Estafeta": 0.65,
        "Redpack": 0.72
    }

    # Ajuste por ruta y servicio
    route_time_boost = np.where(assigned_routes == "Local_Metro", -0.30, np.where(assigned_routes == "Regional_Hub", 0.0, 0.40))
    tier_time_boost = np.where(assigned_tiers == "Express_NextDay", -0.25, np.where(assigned_tiers == "Standard_Ground", 0.0, 0.30))

    transit_hours_raw = np.zeros(num_records)
    for c in carriers:
        mask = (assigned_carriers == c)
        n_c = np.sum(mask)
        if n_c > 0:
            mu = carrier_mean_log[c] + route_time_boost[mask] + tier_time_boost[mask]
            sigma = carrier_sigma_log[c]
            transit_hours_raw[mask] = np.random.lognormal(mean=mu, sigma=sigma, size=n_c)

    transit_hours = np.round(np.maximum(transit_hours_raw, 2.5), 1)

    # 8. Mecanismo de Censura a Derecha (Right-Censoring)
    # Un 12% de los paquetes están actualmente en tránsito (in-flight) al momento de observación
    # No han alcanzado el evento de entrega terminal: conocemos su duración observada hasta hoy
    is_censored = np.random.binomial(1, 0.12, size=num_records)
    
    # Para paquetes censurados, su tiempo observado se trunca antes de la entrega final
    observed_duration = np.where(
        is_censored == 1,
        np.round(transit_hours * np.random.uniform(0.30, 0.85, size=num_records), 1),
        transit_hours
    )

    # 9. Definición Formal del Evento de Supervivencia:
    # Evento = 1 si el paquete NO censurado superó el SLA contractual prometido (SLA Breach).
    # Evento = 0 si se entregó dentro del SLA o si la observación está censurada (aún en camino sin breach definitivo).
    event_sla_breach = np.where(
        (is_censored == 0) & (observed_duration > promised_sla),
        1,
        0
    )

    # Estado operacional
    status = np.where(
        is_censored == 1,
        "IN_TRANSIT_CENSORED",
        np.where(
            event_sla_breach == 1,
            "DELIVERED_SLA_BREACH",
            "DELIVERED_ON_TIME"
        )
    )

    # Costo financiero de penalización (refund/reclamo si hay SLA breach)
    penalty_cost_usd = np.where(
        event_sla_breach == 1,
        np.round(shipping_fee_usd * 1.50 + 12.00, 2),
        0.00
    )

    df = pd.DataFrame({
        "shipment_id": shipment_ids,
        "merchant_id": merchant_ids,
        "carrier": assigned_carriers,
        "service_tier": assigned_tiers,
        "route_type": assigned_routes,
        "distance_km": np.round(distance_km, 1),
        "declared_weight_kg": declared_weight_kg,
        "volumetric_weight_kg": volumetric_weight_kg,
        "billed_weight_kg": billed_weight_kg,
        "has_volumetric_penalty": has_volumetric_penalty,
        "shipping_fee_usd": shipping_fee_usd,
        "promised_sla_hours": promised_sla,
        "duration_hours": observed_duration,
        "event_sla_breach": event_sla_breach,
        "is_censored": is_censored,
        "status": status,
        "penalty_cost_usd": penalty_cost_usd
    })

    # Guardar en formato Parquet columnar optimizado
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    df.to_parquet(output_path, index=False, engine="pyarrow", compression="snappy")

    elapsed = time.perf_counter() - start_time
    breach_rate = (df['event_sla_breach'].sum() / (len(df) - df['is_censored'].sum())) * 100
    print(f"[Data Generator] Successfully generated {len(df):,} shipments in {elapsed:.2f}s -> {output_path}")
    print(f"[Data Generator] Summary: Breach Rate: {breach_rate:.1f}% | Censored: {df['is_censored'].mean()*100:.1f}% | Total Penalties: ${df['penalty_cost_usd'].sum():,.2f} USD")
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Synthetic Logistics Data Generator for Skydropx - Frenet")
    parser.add_argument("--records", type=int, default=50000, help="Number of shipment records to generate")
    parser.add_argument("--output", type=str, default="data/raw_dataset.parquet", help="Output Parquet filepath")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()

    generate_synthetic_dataset(num_records=args.records, output_path=args.output, seed=args.seed)
