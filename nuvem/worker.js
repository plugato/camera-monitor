// camera-nuvem: o que da câmera roda na Cloudflare, e não no Pi.
//
// 1. Histórico (fetch): fotos e clipes dos alertas direto do bucket R2, com o
//    Pi ligado ou não. Atrás do Cloudflare Access; o Worker confere o JWT de
//    novo (sem workers.dev, mas se alguém ligar uma rota sem o Access, barra).
// 2. Vigia (scheduled, a cada 5 min): o aviso de "câmera sem imagem" roda no
//    próprio Pi, então Pi sem luz/internet não avisava ninguém. Aqui o painel é
//    testado de fora, com um service token do Access; 2 falhas seguidas = push.
//    O estado fica no próprio bucket (vigia.json), gravado só quando muda.

const FOTO_NOME = /^\d{8}_\d{6}_\w+\.(?:jpg|mp4)$/;
const DIA = /^\d{8}$/;
const FALHAS_PARA_AVISAR = 2; // 2 checagens de 5 min: reinício do Pi não vira push
const TZ = "America/Sao_Paulo";

export default {
  async fetch(req, env) {
    if (!(await acessoOk(req, env))) {
      return new Response("acesso pelo Cloudflare Access", { status: 403 });
    }
    const url = new URL(req.url);
    if (url.pathname === "/") {
      return new Response(PAGINA.replace("{{PAINEL_URL}}", env.PAINEL_URL), {
        headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
      });
    }
    if (url.pathname === "/api/dia") return listarDia(env, url.searchParams.get("d"));
    if (url.pathname === "/api/vigia") return Response.json(await lerEstado(env));
    if (url.pathname.startsWith("/fotos/")) return midia(req, env, url.pathname.slice(7));
    return new Response("não encontrado", { status: 404 });
  },

  async scheduled(_ev, env, ctx) {
    ctx.waitUntil(vigiar(env));
  },
};

// ------------------------------------------------------------ Cloudflare Access
let certs = { em: 0, chaves: null };

function b64url(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(s + "=".repeat((4 - (s.length % 4)) % 4)), (c) => c.charCodeAt(0));
}

async function chavesAccess(env) {
  if (!certs.chaves || Date.now() - certs.em > 3600_000) {
    const r = await fetch(`${env.ACCESS_TEAM}/cdn-cgi/access/certs`);
    certs = { em: Date.now(), chaves: (await r.json()).keys };
  }
  return certs.chaves;
}

export async function acessoOk(req, env, agora = Date.now()) {
  const token = req.headers.get("cf-access-jwt-assertion");
  if (!token) return false;
  try {
    const [h, p, s] = token.split(".");
    const cab = JSON.parse(new TextDecoder().decode(b64url(h)));
    const jwk = (await chavesAccess(env)).find((k) => k.kid === cab.kid);
    if (!jwk || cab.alg !== "RS256") return false;
    const chave = await crypto.subtle.importKey(
      "jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    const valido = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5", chave, b64url(s), new TextEncoder().encode(`${h}.${p}`));
    if (!valido) return false;
    const d = JSON.parse(new TextDecoder().decode(b64url(p)));
    const aud = Array.isArray(d.aud) ? d.aud : [d.aud];
    return aud.includes(env.ACCESS_AUD) && d.iss === env.ACCESS_TEAM && d.exp * 1000 > agora;
  } catch {
    return false;
  }
}

// ------------------------------------------------------------ histórico
async function listarTudo(env, prefix) {
  const objs = [];
  let cursor;
  do {
    const r = await env.FOTOS.list({ prefix, cursor, limit: 1000 });
    objs.push(...r.objects);
    cursor = r.truncated ? r.cursor : undefined;
  } while (cursor);
  return objs;
}

async function lerEventos(env) {
  try {
    const o = await env.FOTOS.get("eventos.json");
    return o ? await o.json() : [];
  } catch {
    return [];
  }
}

export async function listarDia(env, d) {
  if (!DIA.test(d || "")) return new Response("dia inválido", { status: 400 });
  const [objs, eventos] = await Promise.all([listarTudo(env, `fotos/${d}_`), lerEventos(env)]);
  const nomes = new Set(objs.map((o) => o.key.slice(6)));
  const porFoto = new Map(eventos.filter((e) => e.photo).map((e) => [e.photo, e]));
  const itens = [...nomes]
    .filter((n) => n.endsWith(".jpg"))
    .sort()
    .reverse()
    .map((foto) => {
      const ev = porFoto.get(foto) || {};
      const proprio = foto.slice(0, -4) + ".mp4";
      const video = nomes.has(proprio) ? proprio : ev.video && nomes.has(ev.video) ? ev.video : null;
      return {
        foto,
        hora: `${foto.slice(9, 11)}:${foto.slice(11, 13)}:${foto.slice(13, 15)}`,
        cat: foto.slice(16, -4),
        video,
        dono: video === proprio, // o clipe começou neste alerta: o player fica aqui
        ia: ev.ia || null,
        ia_desc: ev.ia_desc || "",
      };
    });
  return Response.json({ dia: d, itens }, { headers: { "cache-control": "no-store" } });
}

export async function midia(req, env, nome) {
  if (!FOTO_NOME.test(nome)) return new Response("não encontrado", { status: 404 });
  // range: o Safari/iOS só toca vídeo com Range; onlyIf: 304 com If-None-Match
  const obj = await env.FOTOS.get(`fotos/${nome}`, { range: req.headers, onlyIf: req.headers });
  if (obj === null) return new Response("não encontrado", { status: 404 });
  const h = new Headers();
  obj.writeHttpMetadata(h);
  h.set("etag", obj.httpEtag);
  h.set("accept-ranges", "bytes");
  h.set("cache-control", "private, max-age=86400"); // nome com data e hora: não muda
  if (!("body" in obj)) return new Response(null, { status: 304, headers: h });
  if (obj.range && req.headers.has("range")) {
    const ini = "suffix" in obj.range ? obj.size - obj.range.suffix : obj.range.offset ?? 0;
    const len = "suffix" in obj.range ? obj.range.suffix : obj.range.length ?? obj.size - ini;
    h.set("content-range", `bytes ${ini}-${ini + len - 1}/${obj.size}`);
    h.set("content-length", String(len));
    return new Response(obj.body, { status: 206, headers: h });
  }
  h.set("content-length", String(obj.size));
  return new Response(obj.body, { headers: h });
}

// ------------------------------------------------------------ vigia do Pi
async function lerEstado(env) {
  const o = await env.FOTOS.get("vigia.json");
  return o ? await o.json() : { falhas: 0, fora: false, desde: 0, motivo: "" };
}

export async function checarPainel(env) {
  try {
    const r = await fetch(`${env.PAINEL_URL}/healthz`, {
      redirect: "manual",
      signal: AbortSignal.timeout(15000),
      headers: {
        "CF-Access-Client-Id": env.VIGIA_CLIENT_ID,
        "CF-Access-Client-Secret": env.VIGIA_CLIENT_SECRET,
      },
    });
    const texto = (await r.text()).trim();
    if (r.status === 200 && texto === "ok") return { ok: true };
    if ((r.headers.get("location") || "").includes("cloudflareaccess.com")) {
      return { ok: false, acesso: true, motivo: "o Access recusou o token do vigia" };
    }
    return { ok: false, motivo: `HTTP ${r.status}` };
  } catch (e) {
    return { ok: false, motivo: e.name === "TimeoutError" ? "sem resposta em 15 s" : String(e.message || e) };
  }
}

function hora(t) {
  return new Date(t).toLocaleTimeString("pt-BR", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
}

function duracao(ms) {
  const min = Math.round(ms / 60000);
  return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")} min`;
}

async function push(env, titulo, msg, prioridade = "high") {
  if (!env.NTFY_URL) return;
  // JSON (e não cabeçalhos) por causa dos acentos no título
  const u = new URL(env.NTFY_URL);
  await fetch(u.origin, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      topic: u.pathname.slice(1), title: titulo, message: msg, priority: prioridade === "high" ? 4 : 3,
      tags: ["satellite"], click: env.PAINEL_URL,
    }),
  });
}

/** Uma checagem; devolve o push que saiu (ou null). `checar` e `agora` para teste. */
export async function vigiar(env, checar = checarPainel, agora = Date.now()) {
  if (!env.VIGIA_CLIENT_ID || !env.VIGIA_CLIENT_SECRET) {
    console.log("vigia: sem VIGIA_CLIENT_ID/VIGIA_CLIENT_SECRET, nada a fazer");
    return null;
  }
  const antes = await lerEstado(env);
  const r = await checar(env);
  let estado = antes, aviso = null;
  if (r.ok) {
    if (antes.fora) {
      aviso = ["Pi voltou", `O painel voltou a responder. Ficou fora ${duracao(agora - antes.desde)} ` +
                            `(desde ${hora(antes.desde)}).`, "default"];
    }
    estado = { falhas: 0, fora: false, desde: 0, motivo: "" };
  } else {
    const desde = antes.falhas ? antes.desde : agora;
    estado = { falhas: antes.falhas + 1, fora: antes.fora, desde, motivo: r.motivo };
    if (!antes.fora && estado.falhas >= FALHAS_PARA_AVISAR) {
      estado.fora = true;
      aviso = r.acesso
        ? ["Vigia sem acesso ao painel", `${r.motivo}. Confira a policy do app no Access.`]
        : ["Pi fora do ar", `O painel não responde desde ${hora(desde)} (${r.motivo}). ` +
                            "Sem luz, sem internet ou o Pi travou."];
    }
  }
  if (JSON.stringify(estado) !== JSON.stringify(antes)) {
    await env.FOTOS.put("vigia.json", JSON.stringify(estado), {
      httpMetadata: { contentType: "application/json" },
    });
  }
  if (aviso) {
    console.log(`vigia: ${aviso[0]} - ${aviso[1]}`);
    await push(env, ...aviso);
  }
  return aviso;
}

// ------------------------------------------------------------ página
const PAGINA = `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Histórico da Câmera</title><style>
  :root{color-scheme:dark}
  body{margin:0;font-family:system-ui,sans-serif;background:#111827;color:#e5e7eb}
  header{position:sticky;top:0;z-index:5;background:#1f2937;border-bottom:1px solid #374151;
         padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
  h1{font-size:17px;margin:0}
  header a{margin-left:auto;color:#e5e7eb;background:#374151;border-radius:6px;padding:6px 12px;
           text-decoration:none;font-size:14px}
  #vigia{font-size:13px;color:#9ca3af}
  #vigia.fora{color:#fca5a5}
  #dias{display:flex;gap:6px;overflow-x:auto;padding:10px 16px;scrollbar-width:thin}
  #dias button{flex:none;background:#374151;color:#e5e7eb;border:0;border-radius:99px;
               padding:6px 12px;font-size:13px;cursor:pointer}
  #dias button.on{background:#1d4ed8}
  main{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;
       padding:4px 16px 24px;max-width:1300px;margin:auto}
  .card{background:#1f2937;border-radius:8px;padding:10px;font-size:13px}
  .card.descartado{opacity:.55}
  .card time{color:#9ca3af;margin-right:6px}
  .tag{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;margin-right:4px}
  .pessoa{background:#7c2d12}.animal{background:#14532d}
  .card img,.card video{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;
                        border-radius:6px;margin-top:8px;background:#000}
  .card a{color:inherit;font-size:.9em}
  .vazio{color:#6b7280;padding:16px}
  @media(max-width:500px){main{grid-template-columns:1fr}}
</style></head><body>
<header><h1>📼 Histórico da Câmera</h1><span id="vigia"></span>
<a href="{{PAINEL_URL}}">Ao vivo</a></header>
<nav id="dias"></nav>
<main id="lista"><p class="vazio">carregando…</p></main>
<script>
const esc=t=>String(t).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=new Intl.DateTimeFormat('pt-BR',{timeZone:'America/Sao_Paulo',year:'numeric',month:'2-digit',day:'2-digit'});
const rot=new Intl.DateTimeFormat('pt-BR',{timeZone:'America/Sao_Paulo',weekday:'short',day:'2-digit',month:'2-digit'});
const dias=[...Array(30)].map((_,i)=>{const t=new Date(Date.now()-i*864e5);
  const [d,m,a]=fmt.format(t).split('/'); return {id:a+m+d, rot:i===0?'hoje':i===1?'ontem':rot.format(t)};});
const nav=document.getElementById('dias'), lista=document.getElementById('lista');
nav.innerHTML=dias.map(d=>'<button data-d="'+d.id+'">'+d.rot+'</button>').join('');
nav.onclick=e=>{const d=e.target.dataset.d; if(d) abrir(d);};
function card(it){
  const ia=it.ia_desc?' <em>IA: '+(it.ia==='descartado'?'falso alarme, ':'')+esc(it.ia_desc)+'</em>':'';
  const f='/fotos/'+it.foto;
  const midia=it.dono
    ? '<video src="/fotos/'+it.video+'" poster="'+f+'" controls playsinline preload="none"></video><a href="'+f+'" target="_blank">foto</a>'
    : '<a href="'+f+'" target="_blank"><img src="'+f+'" loading="lazy"></a>'
      + (it.video?'<a href="/fotos/'+it.video+'" target="_blank">▶ vídeo</a>':'');
  return '<div class="card'+(it.ia==='descartado'?' descartado':'')+'"><time>'+it.hora+'</time>'
    +'<span class="tag '+esc(it.cat)+'">'+esc(it.cat)+'</span>'+ia+midia+'</div>';
}
async function abrir(d){
  for(const b of nav.children) b.className=b.dataset.d===d?'on':'';
  history.replaceState(null,'','#'+d);
  lista.innerHTML='<p class="vazio">carregando…</p>';
  try{
    const r=await (await fetch('/api/dia?d='+d)).json();
    lista.innerHTML=r.itens.length?r.itens.map(card).join(''):'<p class="vazio">nenhum alerta neste dia</p>';
  }catch(e){ lista.innerHTML='<p class="vazio">erro ao carregar</p>'; }
}
fetch('/api/vigia').then(r=>r.json()).then(v=>{const el=document.getElementById('vigia');
  el.textContent=v.fora?'⚠ Pi fora do ar':'● Pi no ar'; el.className=v.fora?'fora':'';}).catch(()=>{});
const h=location.hash.slice(1);
abrir(dias.some(d=>d.id===h)?h:dias[0].id);
</script></body></html>`;
