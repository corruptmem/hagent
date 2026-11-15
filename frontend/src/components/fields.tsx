import { useState, type ReactNode } from "react";
import type { ModelRef, ValidationIssue } from "../api/types";
import { PROVIDERS } from "../api/types";

interface FieldProps {
  path: string;
  label: string;
  issues?: ValidationIssue[];
  help?: ReactNode;
  children: ReactNode;
}

/** A label, a control, help text and the validation messages of one field. */
export function Field({
  path,
  label,
  issues = [],
  help,
  children,
}: FieldProps) {
  return (
    <div className={issues.length ? "field invalid" : "field"}>
      {label ? <label htmlFor={path}>{label}</label> : null}
      {children}
      {help ? <small className="help">{help}</small> : null}
      {issues.map((issue, i) => (
        <small key={i} className="error" role="alert">
          {issue.msg}
        </small>
      ))}
    </div>
  );
}

interface Common<T> {
  path: string;
  value: T;
  onChange: (value: T) => void;
  disabled?: boolean;
}

export function TextInput({
  path,
  value,
  onChange,
  disabled = false,
  multiline = false,
}: Common<string> & { multiline?: boolean }) {
  return multiline ? (
    <textarea
      id={path}
      data-path={path}
      rows={8}
      value={value}
      disabled={disabled}
      onChange={(e) => {
        onChange(e.target.value);
      }}
    />
  ) : (
    <input
      id={path}
      data-path={path}
      type="text"
      value={value}
      disabled={disabled}
      onChange={(e) => {
        onChange(e.target.value);
      }}
    />
  );
}

export function NumberInput({
  path,
  value,
  onChange,
  disabled = false,
  step = 1,
  min,
  max,
}: Common<number> & { step?: number; min?: number; max?: number }) {
  const [text, setText] = useState(String(value));
  const [last, setLast] = useState(value);
  if (value !== last) {
    setLast(value);
    setText(String(value));
  }
  return (
    <input
      id={path}
      data-path={path}
      type="number"
      step={step}
      min={min}
      max={max}
      value={text}
      disabled={disabled}
      onChange={(e) => {
        setText(e.target.value);
        const parsed = Number(e.target.value);
        if (e.target.value.trim() !== "" && Number.isFinite(parsed)) {
          setLast(parsed);
          onChange(parsed);
        }
      }}
    />
  );
}

export function Select<T extends string>({
  path,
  value,
  onChange,
  options,
  disabled = false,
}: Common<T> & { options: readonly T[] }) {
  return (
    <select
      id={path}
      data-path={path}
      value={value}
      disabled={disabled}
      onChange={(e) => {
        onChange(e.target.value as T);
      }}
    >
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}

export function Checkbox({
  path,
  label,
  value,
  onChange,
  disabled = false,
}: Common<boolean> & { label: string }) {
  return (
    <label className="checkbox">
      <input
        id={path}
        data-path={path}
        type="checkbox"
        checked={value}
        disabled={disabled}
        onChange={(e) => {
          onChange(e.target.checked);
        }}
      />
      {label}
    </label>
  );
}

/**
 * A JSON text area. It calls `onChange` only with valid JSON, and reports
 * whether the text is valid with `onValidity`, so that the form can block
 * the save.
 */
export function JsonField({
  path,
  value,
  onChange,
  onValidity,
  disabled = false,
}: Common<unknown> & { onValidity: (path: string, valid: boolean) => void }) {
  const [text, setText] = useState(() => JSON.stringify(value, null, 2));
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <textarea
        id={path}
        data-path={path}
        className="json"
        rows={6}
        value={text}
        disabled={disabled}
        spellCheck={false}
        onChange={(e) => {
          setText(e.target.value);
          try {
            const parsed: unknown = JSON.parse(e.target.value);
            setError(null);
            onValidity(path, true);
            onChange(parsed);
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
            onValidity(path, false);
          }
        }}
      />
      {error ? (
        <small className="error" role="alert">
          Not valid JSON: {error}
        </small>
      ) : null}
    </>
  );
}

/** A provider and a model name. `null` is "not set" when `optional`. */
export function ModelField({
  path,
  value,
  onChange,
  optional = false,
  emptyLabel = "Not set",
}: Common<ModelRef | null> & { optional?: boolean; emptyLabel?: string }) {
  const enabled = value !== null;
  return (
    <div className="model" data-path={path}>
      {optional ? (
        <label className="checkbox">
          <input
            type="checkbox"
            aria-label={`${path}: set`}
            checked={enabled}
            onChange={(e) => {
              onChange(
                e.target.checked ? { provider: "openai", model: "" } : null,
              );
            }}
          />
          Set
        </label>
      ) : null}
      {value ? (
        <>
          <select
            id={path}
            data-path={`${path}.provider`}
            aria-label={`${path}.provider`}
            value={value.provider}
            onChange={(e) => {
              onChange({
                ...value,
                provider: e.target.value as ModelRef["provider"],
              });
            }}
          >
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <input
            type="text"
            data-path={`${path}.model`}
            aria-label={`${path}.model`}
            placeholder="Model name"
            value={value.model}
            onChange={(e) => {
              onChange({ ...value, model: e.target.value });
            }}
          />
        </>
      ) : (
        <span className="muted">{emptyLabel}</span>
      )}
    </div>
  );
}
