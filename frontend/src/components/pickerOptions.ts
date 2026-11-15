import type { Catalog } from "../api/types";
import type { PickerOption } from "./Picker";

export interface PickerOptions {
  entities: PickerOption[];
  devices: PickerOption[];
  areas: PickerOption[];
  floors: PickerOption[];
  labels: PickerOption[];
  domains: PickerOption[];
  services: PickerOption[];
}

/** Picker options from the HA catalogue. */
export function pickerOptions(catalog: Catalog | undefined): PickerOptions {
  if (!catalog) {
    return {
      entities: [],
      devices: [],
      areas: [],
      floors: [],
      labels: [],
      domains: [],
      services: [],
    };
  }
  const areaName = new Map(catalog.areas.map((a) => [a.id, a.name]));
  const serviceDomains = new Set(
    catalog.services.map((s) => s.split(".")[0] ?? s),
  );
  return {
    entities: catalog.entities.map((e) => {
      const area = e.area_id ? areaName.get(e.area_id) : undefined;
      const hint = [area, e.state, e.disabled ? "disabled" : undefined]
        .filter(Boolean)
        .join(" · ");
      return hint
        ? { value: e.entity_id, label: e.name, hint }
        : { value: e.entity_id, label: e.name };
    }),
    devices: catalog.devices.map((d) => {
      const area = d.area_id ? areaName.get(d.area_id) : undefined;
      return area
        ? { value: d.id, label: d.name, hint: area }
        : { value: d.id, label: d.name };
    }),
    areas: catalog.areas.map((a) => ({ value: a.id, label: a.name })),
    floors: catalog.floors.map((f) => ({ value: f.id, label: f.name })),
    labels: catalog.labels.map((l) => ({ value: l.id, label: l.name })),
    domains: catalog.domains.map((d) => ({ value: d, label: d })),
    services: [
      ...[...serviceDomains]
        .sort()
        .map((d) => ({ value: `${d}.*`, label: `${d}.* (all)` })),
      ...catalog.services.map((s) => ({ value: s, label: s })),
    ],
  };
}
