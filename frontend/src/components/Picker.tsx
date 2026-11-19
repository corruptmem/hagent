import { useId, useMemo, useState } from "react";

export interface PickerOption {
  value: string;
  label: string;
  hint?: string;
}

interface PickerProps {
  path: string;
  label: string;
  options: PickerOption[];
  value: string[];
  onChange: (value: string[]) => void;
  /** Accept a value that is not in `options`, for example when HA is offline. */
  allowCustom?: boolean;
  placeholder?: string;
  disabled?: boolean;
}

const MAX_MATCHES = 50;

/** A multi-select list with search, for entities, devices, areas and more. */
export function Picker({
  path,
  label,
  options,
  value,
  onChange,
  allowCustom = true,
  placeholder = "Search…",
  disabled = false,
}: PickerProps) {
  const [query, setQuery] = useState("");
  const listId = useId();
  const byValue = useMemo(
    () => new Map(options.map((o) => [o.value, o])),
    [options],
  );
  const selected = new Set(value);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return options
      .filter(
        (o) =>
          !selected.has(o.value) &&
          (o.value.toLowerCase().includes(q) ||
            o.label.toLowerCase().includes(q)),
      )
      .slice(0, MAX_MATCHES);
    // `selected` derives from `value`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options, query, value]);

  const add = (item: string) => {
    const trimmed = item.trim();
    if (!trimmed || selected.has(trimmed)) return;
    onChange([...value, trimmed]);
    setQuery("");
  };

  const custom = query.trim();
  const canAddCustom =
    allowCustom &&
    custom !== "" &&
    !selected.has(custom) &&
    !byValue.has(custom);

  return (
    <div className="picker" data-path={path}>
      <span className="picker-label">{label}</span>
      <ul className="chips" aria-label={`${label}: selected`}>
        {value.map((item) => (
          <li key={item} className="chip">
            <span title={item}>{byValue.get(item)?.label ?? item}</span>
            {byValue.get(item) && byValue.get(item)?.label !== item ? (
              <code>{item}</code>
            ) : null}
            <button
              type="button"
              aria-label={`Remove ${item}`}
              disabled={disabled}
              onClick={() => {
                onChange(value.filter((v) => v !== item));
              }}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      <input
        type="search"
        aria-label={`${label}: search`}
        aria-controls={listId}
        placeholder={placeholder}
        value={query}
        disabled={disabled}
        onChange={(e) => {
          setQuery(e.target.value);
        }}
        onKeyDown={(e) => {
          if (e.key !== "Enter") return;
          e.preventDefault();
          const first = matches[0];
          if (first) add(first.value);
          else if (canAddCustom) add(custom);
        }}
      />
      {matches.length > 0 || canAddCustom ? (
        <ul
          id={listId}
          className="matches"
          role="listbox"
          aria-label={`${label}: matches`}
        >
          {matches.map((o) => (
            <li key={o.value} role="option" aria-selected={false}>
              <button
                type="button"
                onClick={() => {
                  add(o.value);
                }}
              >
                <span>{o.label}</span> <code>{o.value}</code>
                {o.hint ? <small> {o.hint}</small> : null}
              </button>
            </li>
          ))}
          {canAddCustom ? (
            <li role="option" aria-selected={false}>
              <button
                type="button"
                onClick={() => {
                  add(custom);
                }}
              >
                Add “{custom}”
              </button>
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}
