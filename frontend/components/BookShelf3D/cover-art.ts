import type { CatalogBook } from "./lensCatalog";
import { siteConfig } from "./site-config";

// Lens does not bundle webfonts, so these resolve against system faces.
const serif = '"Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif';
const sans = '-apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif';

function seeded(seed: string) {
  let state = 2166136261;
  for (const char of seed) {
    state ^= char.charCodeAt(0);
    state = Math.imul(state, 16777619);
  }
  return () => {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function roundedRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  ctx.beginPath();
  ctx.roundRect(x, y, width, height, radius);
}

function wrapText(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  maxWidth: number,
  lineHeight: number,
  maxLines = 8,
) {
  const words = text.split(/\s+/);
  const lines: string[] = [];
  let line = "";

  for (const word of words) {
    const test = line ? `${line} ${word}` : word;
    if (ctx.measureText(test).width > maxWidth && line) {
      lines.push(line);
      line = word;
    } else {
      line = test;
    }
  }

  if (line) lines.push(line);
  lines.slice(0, maxLines).forEach((entry, index) => {
    ctx.fillText(entry, x, y + index * lineHeight);
  });
  return Math.min(lines.length, maxLines);
}

function strokeLine(
  ctx: CanvasRenderingContext2D,
  points: Array<[number, number]>,
  width = 4,
) {
  if (!points.length) return;
  ctx.beginPath();
  ctx.moveTo(points[0][0], points[0][1]);
  for (const [x, y] of points.slice(1)) ctx.lineTo(x, y);
  ctx.lineWidth = width;
  ctx.stroke();
}

function drawMotif(
  ctx: CanvasRenderingContext2D,
  book: CatalogBook,
  width: number,
  height: number,
) {
  const random = seeded(book.id);
  ctx.save();
  ctx.strokeStyle = book.accent;
  ctx.fillStyle = book.accent;
  ctx.globalAlpha = 0.88;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  switch (book.motif) {
    case "lattice": {
      const nodes: Array<[number, number]> = [];
      for (let i = 0; i < 24; i += 1) {
        nodes.push([
          110 + random() * (width - 220),
          380 + random() * (height - 560),
        ]);
      }
      ctx.globalAlpha = 0.38;
      nodes.forEach((point, i) => {
        nodes.slice(i + 1).forEach((other) => {
          const distance = Math.hypot(point[0] - other[0], point[1] - other[1]);
          if (distance < 240) strokeLine(ctx, [point, other], 3);
        });
      });
      ctx.globalAlpha = 0.9;
      nodes.forEach(([x, y], index) => {
        ctx.beginPath();
        ctx.arc(x, y, index % 5 === 0 ? 12 : 6, 0, Math.PI * 2);
        ctx.fill();
      });
      break;
    }
    case "corrosion": {
      ctx.globalAlpha = 0.55;
      for (let ring = 0; ring < 8; ring += 1) {
        ctx.beginPath();
        const cx = width * (0.34 + random() * 0.38);
        const cy = height * (0.38 + random() * 0.42);
        const radius = 40 + ring * 42 + random() * 22;
        for (let a = 0; a <= Math.PI * 2 + 0.2; a += 0.16) {
          const wobble = Math.sin(a * 5 + ring) * 8 + random() * 7;
          const x = cx + Math.cos(a) * (radius + wobble);
          const y = cy + Math.sin(a) * (radius + wobble);
          if (a === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.lineWidth = 4;
        ctx.stroke();
      }
      break;
    }
    case "efficiency": {
      ctx.globalAlpha = 0.72;
      for (let x = 140; x < width - 100; x += 150) {
        ctx.strokeRect(x, 450, 94, height - 690);
        for (let y = 520; y < height - 250; y += 130) {
          ctx.beginPath();
          ctx.arc(x + 47, y, 26, 0, Math.PI * 2);
          ctx.stroke();
          strokeLine(ctx, [
            [x + 10, y],
            [x + 84, y],
          ], 3);
        }
      }
      strokeLine(ctx, [
        [120, height - 315],
        [width - 120, 420],
      ], 9);
      break;
    }
    case "network": {
      ctx.globalAlpha = 0.66;
      const cols = 7;
      const rows = 9;
      for (let x = 0; x < cols; x += 1) {
        for (let y = 0; y < rows; y += 1) {
          const px = 130 + x * 126 + Math.sin(y) * 18;
          const py = 390 + y * 105;
          if (x < cols - 1) {
            strokeLine(ctx, [
              [px, py],
              [px + 126 + Math.sin(y + 1) * 18, py + 105],
            ], 2.5);
          }
          ctx.beginPath();
          ctx.arc(px, py, (x + y) % 5 === 0 ? 11 : 4, 0, Math.PI * 2);
          ctx.fill();
        }
      }
      break;
    }
    case "boom": {
      ctx.globalAlpha = 0.86;
      for (let i = 0; i < 9; i += 1) {
        ctx.beginPath();
        ctx.arc(width * 0.54, height * 0.63, 48 + i * 68, 0, Math.PI * 2);
        ctx.lineWidth = i % 3 === 0 ? 13 : 4;
        ctx.stroke();
      }
      break;
    }
    case "organization": {
      ctx.globalAlpha = 0.8;
      const levels = [1, 3, 5, 7];
      levels.forEach((count, level) => {
        const y = 470 + level * 240;
        for (let i = 0; i < count; i += 1) {
          const x = ((i + 1) * width) / (count + 1);
          roundedRect(ctx, x - 44, y - 32, 88, 64, 12);
          ctx.strokeStyle = book.ink;
          ctx.lineWidth = 4;
          ctx.stroke();
          ctx.strokeStyle = book.accent;
          if (level < levels.length - 1) {
            strokeLine(ctx, [
              [x, y + 32],
              [x, y + 128],
            ], 3);
          }
        }
      });
      break;
    }
    case "schematic": {
      ctx.globalAlpha = 0.72;
      for (let i = 0; i < 6; i += 1) {
        const y = 430 + i * 165;
        strokeLine(ctx, [
          [110, y],
          [300, y],
          [380, y + (i % 2 ? -72 : 72)],
          [610, y + (i % 2 ? -72 : 72)],
          [700, y],
          [width - 110, y],
        ], 5);
        [300, 700].forEach((x) => {
          ctx.beginPath();
          ctx.arc(x, y, 14, 0, Math.PI * 2);
          ctx.fill();
        });
      }
      break;
    }
    case "flight": {
      ctx.globalAlpha = 0.92;
      ctx.beginPath();
      ctx.moveTo(70, height * 0.82);
      ctx.bezierCurveTo(
        width * 0.2,
        height * 0.56,
        width * 0.68,
        height * 0.62,
        width * 0.9,
        height * 0.36,
      );
      ctx.lineWidth = 8;
      ctx.stroke();
      ctx.save();
      ctx.translate(width * 0.87, height * 0.38);
      ctx.rotate(-0.62);
      ctx.beginPath();
      ctx.moveTo(-58, 0);
      ctx.lineTo(40, -26);
      ctx.lineTo(72, 0);
      ctx.lineTo(40, 26);
      ctx.closePath();
      ctx.fill();
      ctx.restore();
      break;
    }
    case "circuit": {
      ctx.globalAlpha = 0.82;
      for (let i = 0; i < 14; i += 1) {
        const y = 390 + i * 76;
        const mid = 250 + random() * 510;
        strokeLine(ctx, [
          [85, y],
          [mid, y],
          [mid, y + (random() > 0.5 ? 42 : -42)],
          [width - 80, y + (random() > 0.5 ? 42 : -42)],
        ], 3);
        ctx.beginPath();
        ctx.arc(mid, y, 9, 0, Math.PI * 2);
        ctx.fill();
      }
      break;
    }
    case "orbit": {
      ctx.globalAlpha = 0.66;
      ctx.save();
      ctx.translate(width * 0.52, height * 0.64);
      for (let i = 0; i < 7; i += 1) {
        ctx.rotate(0.42);
        ctx.beginPath();
        ctx.ellipse(0, 0, 100 + i * 48, 36 + i * 20, 0, 0, Math.PI * 2);
        ctx.lineWidth = i === 3 ? 8 : 3;
        ctx.stroke();
      }
      ctx.restore();
      ctx.beginPath();
      ctx.arc(width * 0.52, height * 0.64, 24, 0, Math.PI * 2);
      ctx.fill();
      break;
    }
    case "branches": {
      ctx.globalAlpha = 0.84;
      const root: [number, number] = [width / 2, height - 185];
      for (let level = 0; level < 5; level += 1) {
        const count = 2 ** level;
        for (let index = 0; index < count; index += 1) {
          const y = height - 240 - level * 180;
          const span = width * (0.13 + level * 0.1);
          const x = width / 2 + (index / Math.max(1, count - 1) - 0.5) * span * 2;
          const parentIndex = Math.floor(index / 2);
          const parentCount = 2 ** Math.max(0, level - 1);
          const parentX =
            level === 0
              ? root[0]
              : width / 2 +
                (parentIndex / Math.max(1, parentCount - 1) - 0.5) *
                  width *
                  (0.13 + (level - 1) * 0.1) *
                  2;
          const parentY = level === 0 ? root[1] : y + 180;
          strokeLine(ctx, [
            [parentX, parentY],
            [x, y],
          ], 4);
          ctx.beginPath();
          ctx.arc(x, y, level === 4 ? 10 : 6, 0, Math.PI * 2);
          ctx.fill();
        }
      }
      break;
    }
    case "wave": {
      ctx.globalAlpha = 0.8;
      for (let row = 0; row < 10; row += 1) {
        ctx.beginPath();
        for (let x = 80; x <= width - 80; x += 12) {
          const y =
            430 +
            row * 92 +
            Math.sin(x * 0.025 + row * 0.78) * (18 + row * 3);
          if (x === 80) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.lineWidth = row % 3 === 0 ? 7 : 3;
        ctx.stroke();
      }
      break;
    }
    case "runner": {
      ctx.globalAlpha = 0.82;
      for (let frame = 0; frame < 7; frame += 1) {
        const x = 150 + frame * 122;
        const y = 760 + Math.sin(frame * 0.8) * 42;
        ctx.beginPath();
        ctx.arc(x, y - 125, 22, 0, Math.PI * 2);
        ctx.fill();
        strokeLine(ctx, [
          [x, y - 98],
          [x + 4, y - 28],
          [x - 38 + frame * 4, y + 42],
        ], 9);
        strokeLine(ctx, [
          [x + 2, y - 70],
          [x + 52, y - 42],
        ], 8);
        strokeLine(ctx, [
          [x + 4, y - 28],
          [x + 48, y + 28],
        ], 9);
      }
      break;
    }
    case "gather": {
      ctx.globalAlpha = 0.76;
      const colors = [book.ink, book.accent, "#2b5f83"];
      for (let i = 0; i < 18; i += 1) {
        ctx.strokeStyle = colors[i % colors.length];
        ctx.lineWidth = 16;
        ctx.beginPath();
        ctx.arc(
          width * (0.2 + random() * 0.6),
          height * (0.34 + random() * 0.48),
          42 + random() * 110,
          0,
          Math.PI * 2,
        );
        ctx.stroke();
      }
      break;
    }
    case "maze": {
      ctx.globalAlpha = 0.76;
      const size = 94;
      for (let x = 92; x < width - 92; x += size) {
        for (let y = 400; y < height - 180; y += size) {
          const open = random() > 0.48;
          strokeLine(ctx, open ? [[x, y], [x + size, y]] : [[x, y], [x, y + size]], 7);
        }
      }
      ctx.strokeStyle = book.accent;
      strokeLine(ctx, [
        [95, height - 215],
        [310, height - 215],
        [310, 680],
        [720, 680],
        [720, 430],
        [width - 95, 430],
      ], 14);
      break;
    }
    case "fracture": {
      ctx.globalAlpha = 0.88;
      for (let i = 0; i < 16; i += 1) {
        const startX = width * 0.5 + (random() - 0.5) * 160;
        const startY = height * 0.62 + (random() - 0.5) * 140;
        const endX = random() > 0.5 ? width - 50 : 50;
        const endY = 340 + random() * (height - 500);
        strokeLine(ctx, [
          [startX, startY],
          [
            (startX + endX) / 2 + (random() - 0.5) * 90,
            (startY + endY) / 2,
          ],
          [endX, endY],
        ], i % 4 === 0 ? 12 : 4);
      }
      break;
    }
    case "continuum": {
      ctx.globalAlpha = 0.88;
      ctx.beginPath();
      ctx.moveTo(70, height * 0.72);
      for (let x = 70; x <= width - 70; x += 16) {
        const t = (x - 70) / (width - 140);
        const y =
          height * 0.72 -
          Math.sin(t * Math.PI * 5) * 90 -
          Math.pow(t, 2) * 330;
        ctx.lineTo(x, y);
      }
      ctx.lineWidth = 12;
      ctx.stroke();
      break;
    }
    case "windows": {
      ctx.globalAlpha = 0.72;
      for (let i = 0; i < 9; i += 1) {
        const inset = 90 + i * 48;
        ctx.strokeRect(
          inset,
          390 + i * 32,
          width - inset * 2,
          height - 590 - i * 64,
        );
        ctx.lineWidth = i === 0 ? 11 : 4;
        ctx.stroke();
      }
      roundedRect(ctx, width * 0.49, height * 0.62, 24, 118, 8);
      ctx.fill();
      break;
    }
    case "steps": {
      ctx.globalAlpha = 0.86;
      for (let i = 0; i < 8; i += 1) {
        ctx.fillRect(120 + i * 92, height - 230 - i * 102, 78, 102 + i * 102);
      }
      ctx.globalAlpha = 0.34;
      strokeLine(ctx, [
        [110, height - 235],
        [width - 115, 420],
      ], 8);
      break;
    }
  }
  ctx.restore();
}

function addPaperGrain(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  seed: string,
) {
  const random = seeded(`${seed}-grain`);
  ctx.save();
  for (let i = 0; i < 1600; i += 1) {
    const alpha = 0.018 + random() * 0.03;
    ctx.fillStyle = random() > 0.5
      ? `rgba(255,255,255,${alpha})`
      : `rgba(0,0,0,${alpha})`;
    const size = random() * 2.2 + 0.35;
    ctx.fillRect(random() * width, random() * height, size, size);
  }
  ctx.restore();
}

function drawCoverTypography(
  ctx: CanvasRenderingContext2D,
  book: CatalogBook,
  width: number,
  height: number,
  decalOnly = false,
) {
  ctx.save();
  ctx.fillStyle = book.ink;
  ctx.textBaseline = "top";
  ctx.letterSpacing = "7px";
  ctx.font = `600 25px ${sans}`;
  ctx.fillText(siteConfig.coverImprint, 82, 74);

  ctx.letterSpacing = "0px";
  const titleSize =
    book.title.length > 38 ? 74 : book.title.length > 24 ? 86 : 112;
  ctx.font = `520 ${titleSize}px ${serif}`;
  const titleY = 142;
  const titleLines = wrapText(
    ctx,
    book.title,
    78,
    titleY,
    width - 156,
    titleSize * 0.91,
    5,
  );

  ctx.font = `520 31px ${sans}`;
  ctx.letterSpacing = "0.2px";
  ctx.fillText(
    book.author,
    82,
    Math.max(320, titleY + titleLines * titleSize * 0.91 + 35),
  );

  if (!decalOnly) {
    ctx.globalAlpha = 0.75;
    ctx.font = `500 20px ${sans}`;
    ctx.letterSpacing = "3px";
    ctx.fillText(siteConfig.coverTagline, 82, height - 90);
  }
  ctx.restore();
}

export function createFrontCover(book: CatalogBook) {
  const logicalWidth = 1024;
  const logicalHeight = 1536;
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 768;
  const ctx = canvas.getContext("2d");
  if (!ctx) return canvas;
  ctx.scale(0.5, 0.5);

  ctx.fillStyle = book.cover;
  ctx.fillRect(0, 0, logicalWidth, logicalHeight);
  drawMotif(ctx, book, logicalWidth, logicalHeight);
  addPaperGrain(ctx, logicalWidth, logicalHeight, book.id);

  ctx.save();
  ctx.globalAlpha = 0.26;
  ctx.strokeStyle = book.ink;
  ctx.lineWidth = 4;
  ctx.strokeRect(22, 22, logicalWidth - 44, logicalHeight - 44);
  ctx.restore();

  drawCoverTypography(ctx, book, logicalWidth, logicalHeight);
  return canvas;
}

export function createTitleDecal(book: CatalogBook) {
  const logicalWidth = 1024;
  const logicalHeight = 1536;
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 768;
  const ctx = canvas.getContext("2d");
  if (!ctx) return canvas;
  ctx.scale(0.5, 0.5);
  drawCoverTypography(ctx, book, logicalWidth, logicalHeight, true);
  return canvas;
}

export function createSpineCover(book: CatalogBook) {
  const logicalWidth = 256;
  const logicalHeight = 2048;
  const canvas = document.createElement("canvas");
  canvas.width = 128;
  canvas.height = 1024;
  const ctx = canvas.getContext("2d");
  if (!ctx) return canvas;
  ctx.scale(0.5, 0.5);

  ctx.fillStyle = book.cover;
  ctx.fillRect(0, 0, logicalWidth, logicalHeight);
  addPaperGrain(ctx, logicalWidth, logicalHeight, `${book.id}-spine`);

  ctx.save();
  ctx.fillStyle = book.accent;
  ctx.fillRect(20, 24, 8, logicalHeight - 48);
  ctx.fillStyle = book.ink;
  ctx.translate(logicalWidth / 2 + 24, logicalHeight - 130);
  ctx.rotate(-Math.PI / 2);
  const size = book.shortTitle.length > 24 ? 68 : 82;
  ctx.font = `560 ${size}px ${serif}`;
  ctx.textBaseline = "middle";
  ctx.fillText(book.shortTitle, 0, 0, 1660);
  ctx.font = `520 35px ${sans}`;
  ctx.fillText(book.author.replace(/^Edited by /, ""), 0, 72, 1450);
  ctx.restore();

  ctx.save();
  ctx.fillStyle = book.ink;
  ctx.font = `700 26px ${sans}`;
  ctx.textAlign = "center";
  ctx.fillText(siteConfig.spineMark, logicalWidth / 2 + 10, logicalHeight - 42);
  ctx.restore();
  return canvas;
}

export function createBackCover(book: CatalogBook) {
  const logicalWidth = 1024;
  const logicalHeight = 1536;
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 768;
  const ctx = canvas.getContext("2d");
  if (!ctx) return canvas;
  ctx.scale(0.5, 0.5);

  ctx.fillStyle = book.cover;
  ctx.fillRect(0, 0, logicalWidth, logicalHeight);
  addPaperGrain(ctx, logicalWidth, logicalHeight, `${book.id}-back`);

  ctx.fillStyle = book.ink;
  ctx.font = `500 45px ${serif}`;
  ctx.textBaseline = "top";
  const lines = wrapText(ctx, book.description, 108, 180, 808, 58, 12);

  ctx.save();
  ctx.globalAlpha = 0.9;
  ctx.fillStyle = book.accent;
  ctx.fillRect(108, 180 + lines * 58 + 78, 108, 9);
  ctx.restore();

  ctx.font = `italic 500 54px ${serif}`;
  wrapText(
    ctx,
    `“${book.quote}”`,
    108,
    180 + lines * 58 + 132,
    808,
    63,
    6,
  );
  ctx.globalAlpha = 0.78;
  ctx.font = `500 20px ${sans}`;
  ctx.letterSpacing = "3px";
  ctx.fillText(
    `${siteConfig.coverImprint} · ${siteConfig.coverTagline}`,
    110,
    1380,
  );
  ctx.restore();
  return canvas;
}
