import assert from "node:assert/strict";
import test from "node:test";
import { readToken } from "./bootstrap.js";

test("launcher URL remains intact for another document to read the same token", () => {
  const launcher = "http://localhost:8787/?t=synthetic%2Btoken&view=compact#settings-network";
  const url = new URL(launcher);
  assert.equal(readToken(url), "synthetic+token");
  assert.equal(url.href, launcher);
  assert.equal(readToken(new URL(url.href)), "synthetic+token");
});

test("first token is read without rewriting duplicate parameters", () => {
  const launcher = "http://localhost:8787/?t=first&t=second#workspace";
  const url = new URL(launcher);
  assert.equal(readToken(url), "first");
  assert.equal(url.href, launcher);
});

test("missing token remains empty and is never invented", () => {
  const url = new URL("http://localhost:8787/");
  assert.equal(readToken(url), "");
  assert.equal(url.href, "http://localhost:8787/");
});
