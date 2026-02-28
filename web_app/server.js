import express from "express";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { parse } from "csv-parse";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const PORT = 5174;

const EVENTS_PATH = path.join(__dirname, "data", "events.csv");
const METRICS_PATH = path.join(__dirname, "data", "metrics.csv");
const IMAGES_DIR = path.join(__dirname, "images");

// ---- Static hosting ----
app.use(express.static(path.join(__dirname, "public")));
app.use("/images", express.static(IMAGES_DIR, { fallthrough: true }));

app.get("/api/health", (req, res) => res.json({ ok: true }));

function fileMtimeIso(p) {
  try {
    const s = fs.statSync(p);
    return s.mtime.toISOString();
  } catch {
    return null;
  }
}

function detectDelimiterFromFirstLine(filePath) {
  try {
    const fd = fs.openSync(filePath, "r");
    const buf = Buffer.alloc(4096);
    const bytes = fs.readSync(fd, buf, 0, buf.length, 0);
    fs.closeSync(fd);

    const firstChunk = buf.slice(0, bytes).toString("utf8");
    const firstLine = firstChunk.split(/\r?\n/).find(Boolean) || "";

    // Count common delimiters; choose the most frequent
    const delims = [",", ":", ";", "\t", "|"];
    let best = ",";
    let bestCount = -1;

    for (const d of delims) {
      const c = firstLine.split(d).length - 1;
      if (c > bestCount) {
        best = d;
        bestCount = c;
      }
    }
    return bestCount > 0 ? best : ","; // fallback
  } catch {
    return ",";
  }
}

function readCsv(filePath) {
  return new Promise((resolve, reject) => {
    const delimiter = detectDelimiterFromFirstLine(filePath);
    const rows = [];

    // If file doesn't exist yet, just return empty
    if (!fs.existsSync(filePath)) return resolve([]);

    fs.createReadStream(filePath)
      .pipe(
        parse({
          columns: true,
          skip_empty_lines: true,
          trim: true,
          delimiter
        })
      )
      .on("data", (row) => rows.push(row))
      .on("end", () => resolve(rows))
      .on("error", (err) => reject(err));
  });
}

// Normalization helpers
function toBool(x) {
  const s = String(x ?? "").trim().toLowerCase();
  return s === "true" || s === "1" || s === "yes" || s === "y";
}

function toNum(x) {
  if (x == null || x === "") return null;
  const n = Number(String(x).replace(",", "."));
  return Number.isFinite(n) ? n : null;
}

function normalizeImagePath(p) {
  const s = String(p ?? "").trim();
  if (!s) return null;

  // If absolute URL, allow it
  if (/^https?:\/\//i.test(s)) return s;

  // Strip leading slashes and any leading "images/" so it maps under /images
  let clean = s.replace(/^\/+/, "");
  clean = clean.replace(/^images\/+/i, "");

  // Prevent directory traversal; keep just path-ish but remove .. segments
  clean = clean.split("/").filter(seg => seg && seg !== "..").join("/");

  return `/images/${encodeURIComponent(clean)}`;
}
function normalizeConfidence(x) {
  const n = toNum(x);
  if (n == null) return null;

  return n;
}

app.get("/api/dashboard", async (req, res) => {
  try {
    const [eventRows, metricRows] = await Promise.all([
      readCsv(EVENTS_PATH),
      readCsv(METRICS_PATH)
    ]);

    const events = eventRows.map((r, i) => {
      const timestamp = r.timestamp ?? r.time ?? r.datetime ?? "";
      const plate = r.number_plate ?? r.plate ?? r.license_plate ?? "";
      const ticketOwned = toBool(r.ticket_owned ?? r.ticket ?? r.has_ticket ?? false);
      const confidence = normalizeConfidence(r.confidence ?? r.conf ?? r.score ?? null);
      const imageUrl = normalizeImagePath(r.image_path ?? r.image ?? r.path ?? r.file ?? null);

      return {
        id: i,
        timestamp,
        plate,
        ticketOwned,
        confidence,
        imageUrl,
        raw: r
      };
    });

    const metrics = metricRows.map((r, i) => {
      const timestamp = r.timestamp ?? r.time ?? r.datetime ?? "";
      return {
        id: i,
        timestamp,
        fps: toNum(r.fps ?? r.FPS),
        latency_ms: toNum(r.latency_ms ?? r.latency ?? r.ms),
        cpu_percent: toNum(r.cpu_percent ?? r.cpu ?? r.cpu_pct),
        temp_c: toNum(r.temp_c ?? r.temp ?? r.temperature_c),
        ram_mb: toNum(r.ram_mb ?? r.ram ?? r.memory_mb),
        dropped_frames: toNum(r.dropped_frames ?? r.dropped ?? null),
        raw: r
      };
    });

    res.json({
      updatedAt: new Date().toISOString(),
      files: {
        eventsMtime: fileMtimeIso(EVENTS_PATH),
        metricsMtime: fileMtimeIso(METRICS_PATH)
      },
      events,
      metrics
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

app.listen(PORT, () => {
  console.log(`Dashboard: http://localhost:${PORT}`);
  console.log(`Events CSV:  ${EVENTS_PATH}`);
  console.log(`Metrics CSV: ${METRICS_PATH}`);
  console.log(`Images dir:  ${IMAGES_DIR} (served under /images)`);
});
