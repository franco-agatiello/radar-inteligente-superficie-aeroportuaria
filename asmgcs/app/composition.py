from __future__ import annotations

from shapely.geometry import Point

from asmgcs.fusion.tracking import AlphaBetaFusionConfig, SurfaceTrackFusionModel
from asmgcs.physics.engine import NetworkXBranchPredictor, SurfacePhysicsEngine
from asmgcs.physics.safety_criteria import SafetyCriteriaRepository, SectorAwareSafetyCriteria
from asmgcs.viewmodels.radar_viewmodel import RadarViewModel
from gis_manager import GISContext, GISManager
from models import GeoReference


def build_radar_viewmodel(
    geo: GeoReference,
    gis_manager: GISManager,
    gis_context: GISContext,
    airport_code: str,
    criteria_repository: SafetyCriteriaRepository,
    fusion_config: AlphaBetaFusionConfig | None = None,
) -> RadarViewModel:
    def is_surface_member(latitude: float, longitude: float) -> bool:
        local_x, local_y = geo.to_local_xy(latitude, longitude)
        return gis_context.drivable_area.contains(Point(local_x, local_y))

    fusion_model = SurfaceTrackFusionModel(
        geo=geo,
        config=fusion_config or AlphaBetaFusionConfig(),
        surface_membership_check=is_surface_member,
    )
    predictor = NetworkXBranchPredictor(
        gis_manager=gis_manager,
        graph=gis_context.graph,
        routing_segments=tuple(gis_context.routing_segments),
    )
    safety_criteria = SectorAwareSafetyCriteria(
        airport_code=airport_code,
        repository=criteria_repository,
        geo=geo,
        sector_areas=gis_context.sector_areas,
    )
    physics_engine = SurfacePhysicsEngine(geo=geo, predictor=predictor, safety_criteria=safety_criteria)
    return RadarViewModel(fusion_model=fusion_model, physics_engine=physics_engine)