import { registerHooks } from "node:module";

function isCssUrl(value) {
  try {
    return new URL(value).pathname.endsWith(".css");
  } catch {
    return false;
  }
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.endsWith(".css")) {
      return {
        shortCircuit: true,
        url: new URL(specifier, context.parentURL).href,
      };
    }
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    if (isCssUrl(url)) {
      return {
        format: "module",
        shortCircuit: true,
        source: "export {};",
      };
    }
    return nextLoad(url, context);
  },
});
