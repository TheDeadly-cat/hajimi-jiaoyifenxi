const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

export function preferredScrollBehavior() {
  return globalThis.matchMedia?.(REDUCED_MOTION_QUERY).matches
    ? "auto"
    : "smooth";
}

