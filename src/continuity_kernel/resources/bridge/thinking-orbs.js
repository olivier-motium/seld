/* Focused MIT-licensed thinking-orb canvas renderer. */

const thinkingOrbStops = new WeakMap();

export function stopThinkingOrb(canvas) {
  thinkingOrbStops.get(canvas)?.();
  thinkingOrbStops.delete(canvas);
}

/*
 * Working and searching modes adapted from thinking-orbs 0.1.1.
 * Source commit 382be79c472cd600277f01e14f98f8c0ee18dcb0, MIT license.
 * The Bridge keeps only the two useful 20px canvas modes and no React runtime.
 */
export function startThinkingOrb(canvas) {
  if (!(canvas instanceof HTMLCanvasElement) || thinkingOrbStops.has(canvas)) return;
  const size = 20;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const context = canvas.getContext("2d");
  if (!context) return;
  canvas.width = Math.round(size * dpr);
  canvas.height = Math.round(size * dpr);

  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let animationFrame = 0;
  let disposed = false;
  let reduced = motion.matches;
  let running = false;
  let visible = true;

  const drawFrame = (time) => {
    const orbState = canvas.dataset.orbState === "working" ? "working" : "searching";
    const speed = orbState === "working" ? 3.9 : 2.665;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, size, size);
    drawThinkingOrb(context, size, time * speed, orbState);
  };

  const stop = () => {
    running = false;
    window.cancelAnimationFrame(animationFrame);
  };

  const loop = () => {
    if (!canvas.isConnected) {
      cleanup();
      return;
    }
    drawFrame(window.performance.now() / 1000);
    if (running) animationFrame = window.requestAnimationFrame(loop);
  };

  const start = () => {
    if (disposed || running || reduced || !visible || document.visibilityState === "hidden") return;
    running = true;
    animationFrame = window.requestAnimationFrame(loop);
  };

  const observer =
    typeof IntersectionObserver === "undefined"
      ? null
      : new IntersectionObserver(([entry]) => {
          visible = entry.isIntersecting;
          if (visible) start();
          else stop();
        });

  const onVisibility = () => {
    if (document.visibilityState === "hidden") stop();
    else start();
  };

  const onMotion = (event) => {
    reduced = event.matches;
    if (reduced) {
      stop();
      drawFrame(0.6);
    } else {
      start();
    }
  };

  function cleanup() {
    if (disposed) return;
    disposed = true;
    stop();
    observer?.disconnect();
    motion.removeEventListener("change", onMotion);
    document.removeEventListener("visibilitychange", onVisibility);
    thinkingOrbStops.delete(canvas);
  }

  thinkingOrbStops.set(canvas, cleanup);
  drawFrame(0.6);
  observer?.observe(canvas);
  motion.addEventListener("change", onMotion);
  document.addEventListener("visibilitychange", onVisibility);
  if (!observer) start();
}

function drawThinkingOrb(context, size, time, orbState) {
  if (orbState === "working") drawWorkingOrb(context, size, time);
  else drawSearchingOrb(context, size, time);
}

function orbHash(a, b) {
  const value = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453;
  return value - Math.floor(value);
}

function orbProjection(yaw, tilt, centerX, centerY, scale) {
  const sinTilt = Math.sin(tilt);
  const cosTilt = Math.cos(tilt);
  const sinYaw = Math.sin(yaw);
  const cosYaw = Math.cos(yaw);
  return (x, y, z) => {
    const rotatedX = x * cosYaw + z * sinYaw;
    const rotatedZ = -x * sinYaw + z * cosYaw;
    const rotatedY = y * cosTilt - rotatedZ * sinTilt;
    const depth = y * sinTilt + rotatedZ * cosTilt;
    return [centerX + rotatedX * scale, centerY - rotatedY * scale, depth];
  };
}

function paintOrbDots(context, dots, minimumRadius = 0.3) {
  dots.sort((left, right) => left.z - right.z);
  for (const dot of dots) {
    const alpha = dot.alpha ?? 1;
    if (alpha < 0.02) continue;
    const white = Math.min(1, Math.max(0, dot.white));
    const gray = Math.round(white * 255);
    context.fillStyle = `rgba(${gray},${gray},${gray},${alpha})`;
    context.beginPath();
    context.arc(dot.x, dot.y, Math.max(minimumRadius, dot.radius), 0, Math.PI * 2);
    context.fill();
  }
}

function orbRadiusScale(size, exponent) {
  return (size / 300) ** exponent;
}

function drawWorkingOrb(context, size, time) {
  const center = size / 2;
  const radius = (size / 2) * 0.82;
  const project = orbProjection(time * 0.12, 0.3, center, center, 1);
  const radiusScale = orbRadiusScale(size, 0.6);
  const dots = [];

  for (let orbit = 0; orbit < 3; orbit += 1) {
    const first = orbHash(orbit, 1.7);
    const second = orbHash(orbit, 5.2);
    const third = orbHash(orbit, 8.9);
    const orbitRadius = radius * (0.45 + 0.52 * first);
    const theta = first * 2 * Math.PI;
    const phi = Math.acos(2 * second - 1);
    const normalX = Math.sin(phi) * Math.cos(theta);
    const normalY = Math.cos(phi);
    const normalZ = Math.sin(phi) * Math.sin(theta);
    let basisX = -normalY;
    let basisY = normalX;
    const basisZ = 0;
    const basisLength = Math.max(1e-6, Math.sqrt(basisX * basisX + basisY * basisY));
    basisX /= basisLength;
    basisY /= basisLength;
    const crossX = normalY * basisZ - normalZ * basisY;
    const crossY = normalZ * basisX - normalX * basisZ;
    const crossZ = normalX * basisY - normalY * basisX;
    const speed = (0.25 + 0.55 * third) * (third > 0.5 ? 1 : -1);

    for (let point = 0; point < 10; point += 1) {
      const angle = (point / 10) * 2 * Math.PI;
      const [x, y, z] = project(
        (basisX * Math.cos(angle) + crossX * Math.sin(angle)) * orbitRadius,
        (basisY * Math.cos(angle) + crossY * Math.sin(angle)) * orbitRadius,
        (basisZ * Math.cos(angle) + crossZ * Math.sin(angle)) * orbitRadius,
      );
      const depth = (z / orbitRadius + 1) / 2;
      dots.push({
        alpha: 0.5 * (0.4 + 0.6 * depth),
        radius: 2.16 * radiusScale,
        white: 0.72,
        x,
        y,
        z,
      });
    }

    for (let particle = 0; particle < 3; particle += 1) {
      const angle = time * speed + (particle / 3) * 2 * Math.PI + second * 6;
      const [x, y, z] = project(
        (basisX * Math.cos(angle) + crossX * Math.sin(angle)) * orbitRadius,
        (basisY * Math.cos(angle) + crossY * Math.sin(angle)) * orbitRadius,
        (basisZ * Math.cos(angle) + crossZ * Math.sin(angle)) * orbitRadius,
      );
      const depth = (z / orbitRadius + 1) / 2;
      dots.push({
        radius: (2.88 + 3.84 * depth) * radiusScale,
        white: 0.3 - 0.22 * depth,
        x,
        y,
        z,
      });
    }
  }
  paintOrbDots(context, dots);
}

function drawSearchingOrb(context, size, time) {
  const spin = 0.5;
  const center = size / 2;
  const radius = (size / 2) * 0.82;
  const tilt = 0.4 + 0.06 * Math.sin(time * 0.35);
  const project = orbProjection(time * spin, tilt, center, center, radius);
  const scan = time * (spin + (1.7 - spin) * 4.335);
  const radiusScale = orbRadiusScale(size, 0.6);
  const dots = [];

  for (let ring = 0; ring <= 6; ring += 1) {
    const latitude = -Math.PI / 2 + (ring / 6) * Math.PI;
    const cosLatitude = Math.cos(latitude);
    const sinLatitude = Math.sin(latitude);
    const longitudeCount = Math.max(1, Math.round(Math.abs(cosLatitude) * 14));
    for (let point = 0; point < longitudeCount; point += 1) {
      const longitude = (point / longitudeCount) * 2 * Math.PI;
      const [x, y, z] = project(
        cosLatitude * Math.cos(longitude),
        sinLatitude,
        cosLatitude * Math.sin(longitude),
      );
      const depth = (z + 1) / 2;
      const delta = Math.atan2(
        Math.sin(longitude + time * spin - scan),
        Math.cos(longitude + time * spin - scan),
      );
      const boost = Math.exp(-(delta * delta) / 0.18) * Math.max(0, z);
      dots.push({
        alpha: 0.45 + 0.55 * Math.min(1, boost),
        radius: (1.05 + 2.975 * depth + 1.75 * boost) * radiusScale,
        white: 0.62 - 0.54 * depth,
        x,
        y,
        z,
      });
    }
  }
  paintOrbDots(context, dots, 0.525);
}
