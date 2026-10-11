// maw phone-approval relay (refit R3b): the Cloudflare Worker entry. It exports
// ONLY `default`: workerd rejects non-function named exports on the main module
// (caught by `wrangler dev`, not by the node harness). Logic: handler.mjs.
import { handle, kvStore } from "./handler.mjs";

export default {
  fetch(request, env) {
    return handle(request, env, kvStore(env.RELAY_KV));
  },
};
