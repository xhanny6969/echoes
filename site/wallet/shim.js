/**
 * Browser shims for the wallet bundle.
 *
 * @near-wallet-selector pulls in node-oriented code paths (borsh/safe-buffer,
 * @here-wallet/core's crypto helpers). esbuild has no idea this is for a browser,
 * so import this shim first and alias the node builtins to their browser
 * equivalents (see the build:wallet script in package.json).
 */
import { Buffer } from "buffer";
import process from "process";

if (typeof globalThis.Buffer === "undefined") globalThis.Buffer = Buffer;
if (typeof globalThis.process === "undefined") globalThis.process = process;
if (typeof globalThis.global === "undefined") globalThis.global = globalThis;

export {};
