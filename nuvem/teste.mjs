// node nuvem/teste.mjs: lógica do Worker com R2 e fetch falsos (sem rede).
import assert from "node:assert/strict";
import { acessoOk, listarDia, midia, vigiar } from "./worker.js";

// R2 em memória, com o que o Worker usa: get (range/onlyIf), list, put
function r2(arquivos) {
  const m = new Map(Object.entries(arquivos));
  return {
    m,
    async list({ prefix }) {
      return { objects: [...m.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key })), truncated: false };
    },
    async get(key, op = {}) {
      if (!m.has(key)) return null;
      const dados = new TextEncoder().encode(m.get(key));
      const base = { size: dados.length, httpEtag: '"e1"', writeHttpMetadata: (h) => h.set("content-type", "video/mp4") };
      if (op.onlyIf?.get?.("if-none-match") === '"e1"') return base;
      const rg = op.range?.get?.("range")?.match(/bytes=(\d+)-(\d*)/);
      let corpo = dados, range;
      if (rg) {
        const ini = +rg[1], fim = rg[2] ? +rg[2] : dados.length - 1;
        corpo = dados.slice(ini, fim + 1); range = { offset: ini, length: corpo.length };
      }
      const txt = new TextDecoder().decode(corpo);
      return { ...base, range, body: corpo, json: async () => JSON.parse(txt) };
    },
    async put(key, v) { m.set(key, v); },
  };
}

// --- histórico
const env = { FOTOS: r2({
  "fotos/20261113_223135_pessoa.jpg": "j", "fotos/20261113_223135_pessoa.mp4": "0123456789",
  "fotos/20261113_223157_pessoa.jpg": "j", "fotos/20261113_100000_animal.jpg": "j",
  "fotos/20261114_000000_pessoa.jpg": "j",
  "eventos.json": JSON.stringify([
    { photo: "20261113_223157_pessoa.jpg", ia: "descartado", ia_desc: "nada", video: "20261113_223135_pessoa.mp4" },
    { photo: "20261113_223135_pessoa.jpg", ia: "confirmado", ia_desc: "homem", video: "20261113_223135_pessoa.mp4" }]),
}) };
let r = await (await listarDia(env, "20261113")).json();
assert.deepEqual(r.itens.map((i) => [i.foto, i.video, i.dono, i.ia]), [
  ["20261113_223157_pessoa.jpg", "20261113_223135_pessoa.mp4", false, "descartado"],
  ["20261113_223135_pessoa.jpg", "20261113_223135_pessoa.mp4", true, "confirmado"],
  ["20261113_100000_animal.jpg", null, false, null]]);
assert.equal(r.itens[2].hora, "10:00:00");
assert.equal(r.itens[2].cat, "animal");
assert.equal((await listarDia(env, "../x")).status, 400);

const pedir = (h = {}) => new Request("https://h/fotos/x", { headers: h });
let m = await midia(pedir(), env, "20261113_223135_pessoa.mp4");
assert.equal(m.status, 200); assert.equal(await m.text(), "0123456789");
m = await midia(pedir({ range: "bytes=2-5" }), env, "20261113_223135_pessoa.mp4");
assert.equal(m.status, 206); assert.equal(m.headers.get("content-range"), "bytes 2-5/10"); assert.equal(await m.text(), "2345");
assert.equal((await midia(pedir({ "if-none-match": '"e1"' }), env, "20261113_223135_pessoa.mp4")).status, 304);
assert.equal((await midia(pedir(), env, "../eventos.json")).status, 404);
assert.equal((await midia(pedir(), env, "vigia.json")).status, 404);

// --- vigia: 1 falha não avisa, 2 avisam uma vez, a volta avisa com a duração
const pushes = [];
globalThis.fetch = async (url, op) => { pushes.push(JSON.parse(op.body)); return new Response("{}"); };
const ve = { FOTOS: r2({}), VIGIA_CLIENT_ID: "i", VIGIA_CLIENT_SECRET: "s", NTFY_URL: "https://ntfy.sh/topico-x", PAINEL_URL: "https://cam" };
const t0 = Date.UTC(2026, 10, 14, 15, 0); // 12:00 em São Paulo
const cai = async () => ({ ok: false, motivo: "HTTP 530" }), sobe = async () => ({ ok: true });
assert.equal(await vigiar({ ...ve, VIGIA_CLIENT_ID: "" }, cai, t0), null); // sem token: não roda
assert.equal(await vigiar(ve, sobe, t0), null);
assert.equal(ve.FOTOS.m.has("vigia.json"), false); // tudo bem e nada mudou: não grava
assert.equal(await vigiar(ve, cai, t0 + 300e3), null);
assert.equal((await vigiar(ve, cai, t0 + 600e3))[0], "Pi fora do ar");
assert.equal(await vigiar(ve, cai, t0 + 900e3), null); // segue fora: não repete
const volta = await vigiar(ve, sobe, t0 + 300e3 + 75 * 60e3);
assert.equal(volta[0], "Pi voltou");
assert.match(volta[1], /Ficou fora 1 h 15 min \(desde 12:05\)/);
assert.equal(pushes.length, 2);
assert.equal(pushes[0].topic, "topico-x"); assert.match(pushes[0].message, /desde 12:05 \(HTTP 530\)/);
const negado = async () => ({ ok: false, acesso: true, motivo: "o Access recusou o token do vigia" });
await vigiar(ve, negado, t0); assert.equal((await vigiar(ve, negado, t0))[0], "Vigia sem acesso ao painel");

// --- Access: JWT assinado aqui, certs falsos
const { publicKey, privateKey } = await crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
  true, ["sign", "verify"]);
const jwk = { ...(await crypto.subtle.exportKey("jwk", publicKey)), kid: "k1" };
globalThis.fetch = async () => Response.json({ keys: [jwk] });
const b64 = (o) => Buffer.from(typeof o === "string" ? o : JSON.stringify(o)).toString("base64url");
async function jwt(dados, kid = "k1") {
  const ini = `${b64({ alg: "RS256", kid })}.${b64(dados)}`;
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", privateKey, new TextEncoder().encode(ini));
  return `${ini}.${Buffer.from(sig).toString("base64url")}`;
}
const ae = { ACCESS_TEAM: "https://time.cloudflareaccess.com", ACCESS_AUD: "aud1" };
const comJwt = (t) => new Request("https://h/", { headers: { "cf-access-jwt-assertion": t } });
const bom = { aud: ["aud1"], iss: ae.ACCESS_TEAM, exp: 2e9 };
assert.equal(await acessoOk(comJwt(await jwt(bom)), ae, 1e12), true);
assert.equal(await acessoOk(comJwt(await jwt({ ...bom, aud: ["outro"] })), ae, 1e12), false);
assert.equal(await acessoOk(comJwt(await jwt({ ...bom, exp: 1 })), ae, 1e12), false);
assert.equal(await acessoOk(comJwt(await jwt(bom, "k2")), ae, 1e12), false);
const t = await jwt(bom);
assert.equal(await acessoOk(comJwt(t.slice(0, -4) + "AAAA"), ae, 1e12), false);
assert.equal(await acessoOk(new Request("https://h/"), ae, 1e12), false);
console.log("nuvem: ok");
