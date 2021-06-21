from __future__ import annotations

import math
import operator
from collections import Iterator, Iterable
from typing import TypeVar, TYPE_CHECKING, Any

from game.theater import (
    ControlPoint,
    OffMapSpawn,
    TheaterGroundObject,
    MissionTarget,
    Fob,
    FrontLine,
    Airfield,
)
from game.theater.theatergroundobject import (
    EwrGroundObject,
    SamGroundObject,
    VehicleGroupGroundObject,
    NavalGroundObject,
    BuildingGroundObject,
    IadsGroundObject,
)
from game.transfers import CargoShip, Convoy
from game.utils import meters, nautical_miles, Distance
from gen.flights.closestairfields import ObjectiveDistanceCache, ClosestAirfields

if TYPE_CHECKING:
    from game import Game

MissionTargetType = TypeVar("MissionTargetType", bound=MissionTarget)


class ObjectiveFinder:
    """Identifies potential objectives for the mission planner."""

    # TODO: Merge into doctrine.
    AIRFIELD_THREAT_RANGE = nautical_miles(150)
    SAM_THREAT_RANGE = nautical_miles(100)

    def __init__(self, game: Game, is_player: bool) -> None:
        self.game = game
        self.is_player = is_player

    def enemy_air_defenses(self) -> Iterator[tuple[IadsGroundObject, Distance]]:
        """Iterates over all enemy SAM sites."""
        doctrine = self.game.faction_for(self.is_player).doctrine
        threat_zones = self.game.threat_zone_for(not self.is_player)
        for cp in self.enemy_control_points():
            for ground_object in cp.ground_objects:
                if ground_object.is_dead:
                    continue

                if isinstance(ground_object, EwrGroundObject):
                    if threat_zones.threatened_by_air_defense(ground_object):
                        # This is a very weak heuristic for determining whether the EWR
                        # is close enough to be worth targeting before a SAM that is
                        # covering it. Ingress distance corresponds to the beginning of
                        # the attack range and is sufficient for most standoff weapons,
                        # so treating the ingress distance as the threat distance sorts
                        # these EWRs such that they will be attacked before SAMs that do
                        # not threaten the ingress point, but after those that do.
                        target_range = doctrine.ingress_egress_distance
                    else:
                        # But if the EWR isn't covered then we should only be worrying
                        # about its detection range.
                        target_range = ground_object.max_detection_range()
                elif isinstance(ground_object, SamGroundObject):
                    target_range = ground_object.max_threat_range()
                else:
                    continue

                yield ground_object, target_range

    def threatening_air_defenses(self) -> Iterator[IadsGroundObject]:
        """Iterates over enemy SAMs in threat range of friendly control points.

        SAM sites are sorted by their closest proximity to any friendly control
        point (airfield or fleet).
        """

        target_ranges: list[tuple[IadsGroundObject, Distance]] = []
        for target, threat_range in self.enemy_air_defenses():
            ranges: list[Distance] = []
            for cp in self.friendly_control_points():
                ranges.append(meters(target.distance_to(cp)) - threat_range)
            target_ranges.append((target, min(ranges)))

        target_ranges = sorted(target_ranges, key=operator.itemgetter(1))
        for target, _range in target_ranges:
            yield target

    def enemy_vehicle_groups(self) -> Iterator[VehicleGroupGroundObject]:
        """Iterates over all enemy vehicle groups."""
        for cp in self.enemy_control_points():
            for ground_object in cp.ground_objects:
                if not isinstance(ground_object, VehicleGroupGroundObject):
                    continue

                if ground_object.is_dead:
                    continue

                yield ground_object

    def threatening_vehicle_groups(self) -> Iterator[VehicleGroupGroundObject]:
        """Iterates over enemy vehicle groups near friendly control points.

        Groups are sorted by their closest proximity to any friendly control
        point (airfield or fleet).
        """
        return self._targets_by_range(self.enemy_vehicle_groups())

    def enemy_ships(self) -> Iterator[NavalGroundObject]:
        for cp in self.enemy_control_points():
            for ground_object in cp.ground_objects:
                if not isinstance(ground_object, NavalGroundObject):
                    continue

                if ground_object.is_dead:
                    continue

                yield ground_object

    def threatening_ships(self) -> Iterator[NavalGroundObject]:
        """Iterates over enemy ships near friendly control points.

        Groups are sorted by their closest proximity to any friendly control
        point (airfield or fleet).
        """
        return self._targets_by_range(self.enemy_ships())

    def _targets_by_range(
        self, targets: Iterable[MissionTargetType]
    ) -> Iterator[MissionTargetType]:
        target_ranges: list[tuple[MissionTargetType, float]] = []
        for target in targets:
            ranges: list[float] = []
            for cp in self.friendly_control_points():
                ranges.append(target.distance_to(cp))
            target_ranges.append((target, min(ranges)))

        target_ranges = sorted(target_ranges, key=operator.itemgetter(1))
        for target, _range in target_ranges:
            yield target

    def strike_targets(self) -> Iterator[TheaterGroundObject[Any]]:
        """Iterates over enemy strike targets.

        Targets are sorted by their closest proximity to any friendly control
        point (airfield or fleet).
        """
        targets: list[tuple[TheaterGroundObject[Any], float]] = []
        # Building objectives are made of several individual TGOs (one per
        # building).
        found_targets: set[str] = set()
        for enemy_cp in self.enemy_control_points():
            for ground_object in enemy_cp.ground_objects:
                # TODO: Reuse ground_object.mission_types.
                # The mission types for ground objects are currently not
                # accurate because we include things like strike and BAI for all
                # targets since they have different planning behavior (waypoint
                # generation is better for players with strike when the targets
                # are stationary, AI behavior against weaker air defenses is
                # better with BAI), so that's not a useful filter. Once we have
                # better control over planning profiles and target dependent
                # loadouts we can clean this up.
                if isinstance(ground_object, VehicleGroupGroundObject):
                    # BAI target, not strike target.
                    continue

                if isinstance(ground_object, NavalGroundObject):
                    # Anti-ship target, not strike target.
                    continue

                if isinstance(ground_object, SamGroundObject):
                    # SAMs are targeted by DEAD. No need to double plan.
                    continue

                is_building = isinstance(ground_object, BuildingGroundObject)
                is_fob = isinstance(enemy_cp, Fob)
                if is_building and is_fob and ground_object.is_control_point:
                    # This is the FOB structure itself. Can't be repaired or
                    # targeted by the player, so shouldn't be targetable by the
                    # AI.
                    continue

                if ground_object.is_dead:
                    continue
                if ground_object.name in found_targets:
                    continue
                ranges: list[float] = []
                for friendly_cp in self.friendly_control_points():
                    ranges.append(ground_object.distance_to(friendly_cp))
                targets.append((ground_object, min(ranges)))
                found_targets.add(ground_object.name)
        targets = sorted(targets, key=operator.itemgetter(1))
        for target, _range in targets:
            yield target

    def front_lines(self) -> Iterator[FrontLine]:
        """Iterates over all active front lines in the theater."""
        yield from self.game.theater.conflicts()

    def vulnerable_control_points(self) -> Iterator[ControlPoint]:
        """Iterates over friendly CPs that are vulnerable to enemy CPs.

        Vulnerability is defined as any enemy CP within threat range of of the
        CP.
        """
        for cp in self.friendly_control_points():
            if isinstance(cp, OffMapSpawn):
                # Off-map spawn locations don't need protection.
                continue
            airfields_in_proximity = self.closest_airfields_to(cp)
            airfields_in_threat_range = (
                airfields_in_proximity.operational_airfields_within(
                    self.AIRFIELD_THREAT_RANGE
                )
            )
            for airfield in airfields_in_threat_range:
                if not airfield.is_friendly(self.is_player):
                    yield cp
                    break

    def oca_targets(self, min_aircraft: int) -> Iterator[ControlPoint]:
        airfields = []
        for control_point in self.enemy_control_points():
            if not isinstance(control_point, Airfield):
                continue
            if control_point.base.total_aircraft >= min_aircraft:
                airfields.append(control_point)
        return self._targets_by_range(airfields)

    def convoys(self) -> Iterator[Convoy]:
        for front_line in self.front_lines():
            yield from self.game.transfers.convoys.travelling_to(
                front_line.control_point_hostile_to(self.is_player)
            )

    def cargo_ships(self) -> Iterator[CargoShip]:
        for front_line in self.front_lines():
            yield from self.game.transfers.cargo_ships.travelling_to(
                front_line.control_point_hostile_to(self.is_player)
            )

    def friendly_control_points(self) -> Iterator[ControlPoint]:
        """Iterates over all friendly control points."""
        return (
            c for c in self.game.theater.controlpoints if c.is_friendly(self.is_player)
        )

    def farthest_friendly_control_point(self) -> ControlPoint:
        """Finds the friendly control point that is farthest from any threats."""
        threat_zones = self.game.threat_zone_for(not self.is_player)

        farthest = None
        max_distance = meters(0)
        for cp in self.friendly_control_points():
            if isinstance(cp, OffMapSpawn):
                continue
            distance = threat_zones.distance_to_threat(cp.position)
            if distance > max_distance:
                farthest = cp
                max_distance = distance

        if farthest is None:
            raise RuntimeError("Found no friendly control points. You probably lost.")
        return farthest

    def closest_friendly_control_point(self) -> ControlPoint:
        """Finds the friendly control point that is closest to any threats."""
        threat_zones = self.game.threat_zone_for(not self.is_player)

        closest = None
        min_distance = meters(math.inf)
        for cp in self.friendly_control_points():
            if isinstance(cp, OffMapSpawn):
                continue
            distance = threat_zones.distance_to_threat(cp.position)
            if distance < min_distance:
                closest = cp
                min_distance = distance

        if closest is None:
            raise RuntimeError("Found no friendly control points. You probably lost.")
        return closest

    def enemy_control_points(self) -> Iterator[ControlPoint]:
        """Iterates over all enemy control points."""
        return (
            c
            for c in self.game.theater.controlpoints
            if not c.is_friendly(self.is_player)
        )

    def all_possible_targets(self) -> Iterator[MissionTarget]:
        """Iterates over all possible mission targets in the theater.

        Valid mission targets are control points (airfields and carriers), front
        lines, and ground objects (SAM sites, factories, resource extraction
        sites, etc).
        """
        for cp in self.game.theater.controlpoints:
            yield cp
            yield from cp.ground_objects
        yield from self.front_lines()

    @staticmethod
    def closest_airfields_to(location: MissionTarget) -> ClosestAirfields:
        """Returns the closest airfields to the given location."""
        return ObjectiveDistanceCache.get_closest_airfields(location)
