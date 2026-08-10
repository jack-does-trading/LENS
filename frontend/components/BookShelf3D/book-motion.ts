export type BookPose = {
  x: number;
  z: number;
  yaw: number;
  scale: number;
};

export type BookFootprint = BookPose & {
  id: string;
  width: number;
  thickness: number;
};

export type MotionBookSize = {
  width: number;
  thickness: number;
};

export type MotionLayout = {
  shelvedZ: number;
  presentedZ: number;
  rotationLaneZ: number;
  presentedScale: number;
  collisionMargin: number;
};

export type BrowseMotionPhase =
  | "retreat-current"
  | "turn-current"
  | "shelve-current"
  | "extract-next"
  | "turn-next"
  | "settle-next";

export const shelvedYaw = Math.PI / 2;
export const presentedYaw = 0;

const shelvedZ = -0.64;
const presentedZ = 0.4;
const presentedScale = 1.035;
const maximumFocusScale = 1.08;
const collisionMargin = 0.035;

export const browsePhaseDuration: Record<BrowseMotionPhase, number> = {
  "retreat-current": 0.11,
  "turn-current": 0.14,
  "shelve-current": 0.13,
  "extract-next": 0.13,
  "turn-next": 0.14,
  "settle-next": 0.11,
};

function clamp01(value: number) {
  return Math.min(1, Math.max(0, value));
}

function smooth(value: number) {
  const t = clamp01(value);
  return t * t * (3 - 2 * t);
}

function lerp(start: number, end: number, amount: number) {
  return start + (end - start) * amount;
}

export function createMotionLayout(books: MotionBookSize[]): MotionLayout {
  const maxShelvedHalfDepth = books.reduce(
    (maximum, book) => Math.max(maximum, book.width * 0.5),
    0,
  );
  const maxRotationRadius = books.reduce(
    (maximum, book) =>
      Math.max(
        maximum,
        Math.hypot(book.width, book.thickness) *
          0.5 *
          maximumFocusScale,
      ),
    0,
  );

  return {
    shelvedZ,
    presentedZ,
    rotationLaneZ:
      shelvedZ +
      maxShelvedHalfDepth +
      maxRotationRadius +
      collisionMargin,
    presentedScale,
    collisionMargin,
  };
}

export function shelvedBookPose(layout: MotionLayout): BookPose {
  return {
    x: 0,
    z: layout.shelvedZ,
    yaw: shelvedYaw,
    scale: 1,
  };
}

export function presentedBookPose(layout: MotionLayout): BookPose {
  return {
    x: 0,
    z: layout.presentedZ,
    yaw: presentedYaw,
    scale: layout.presentedScale,
  };
}

export function browseMotionPose(
  phase: BrowseMotionPhase,
  progress: number,
  layout: MotionLayout,
): BookPose {
  const t = smooth(progress);

  switch (phase) {
    case "retreat-current":
      return {
        x: 0,
        z: lerp(layout.presentedZ, layout.rotationLaneZ, t),
        yaw: presentedYaw,
        scale: lerp(layout.presentedScale, 1, t),
      };
    case "turn-current":
      return {
        x: 0,
        z: layout.rotationLaneZ,
        yaw: lerp(presentedYaw, shelvedYaw, t),
        scale: 1,
      };
    case "shelve-current":
      return {
        x: 0,
        z: lerp(layout.rotationLaneZ, layout.shelvedZ, t),
        yaw: shelvedYaw,
        scale: 1,
      };
    case "extract-next":
      return {
        x: 0,
        z: lerp(layout.shelvedZ, layout.rotationLaneZ, t),
        yaw: shelvedYaw,
        scale: 1,
      };
    case "turn-next":
      return {
        x: 0,
        z: layout.rotationLaneZ,
        yaw: lerp(shelvedYaw, presentedYaw, t),
        scale: 1,
      };
    case "settle-next":
      return {
        x: 0,
        z: lerp(layout.rotationLaneZ, layout.presentedZ, t),
        yaw: presentedYaw,
        scale: lerp(1, layout.presentedScale, t),
      };
  }
}

export function focusedBookPose(
  progress: number,
  layout: MotionLayout,
  focusX: number,
  focusZ: number,
  focusScale: number,
): BookPose {
  const value = clamp01(progress);
  const clearanceProgress = smooth(Math.min(1, value / 0.55));
  const presentationProgress = smooth(
    Math.max(0, (value - 0.55) / 0.45),
  );

  return {
    x: lerp(0, focusX, presentationProgress),
    z: lerp(layout.presentedZ, focusZ, clearanceProgress),
    yaw: presentedYaw,
    scale: lerp(
      layout.presentedScale,
      focusScale,
      presentationProgress,
    ),
  };
}

type Axis = { x: number; z: number };

function dot(left: Axis, right: Axis) {
  return left.x * right.x + left.z * right.z;
}

function axesFor(footprint: BookFootprint) {
  const cosine = Math.cos(footprint.yaw);
  const sine = Math.sin(footprint.yaw);
  return {
    width: { x: cosine, z: -sine },
    thickness: { x: sine, z: cosine },
  };
}

export function bookFootprintsOverlap(
  left: BookFootprint,
  right: BookFootprint,
  margin = collisionMargin,
) {
  const leftAxes = axesFor(left);
  const rightAxes = axesFor(right);
  const axes = [
    leftAxes.width,
    leftAxes.thickness,
    rightAxes.width,
    rightAxes.thickness,
  ];
  const centerDelta = {
    x: right.x - left.x,
    z: right.z - left.z,
  };
  const leftHalfWidth = left.width * left.scale * 0.5 + margin * 0.5;
  const leftHalfThickness =
    left.thickness * left.scale * 0.5 + margin * 0.5;
  const rightHalfWidth =
    right.width * right.scale * 0.5 + margin * 0.5;
  const rightHalfThickness =
    right.thickness * right.scale * 0.5 + margin * 0.5;

  return axes.every((axis) => {
    const distance = Math.abs(dot(centerDelta, axis));
    const leftRadius =
      leftHalfWidth * Math.abs(dot(leftAxes.width, axis)) +
      leftHalfThickness * Math.abs(dot(leftAxes.thickness, axis));
    const rightRadius =
      rightHalfWidth * Math.abs(dot(rightAxes.width, axis)) +
      rightHalfThickness * Math.abs(dot(rightAxes.thickness, axis));
    return distance < leftRadius + rightRadius;
  });
}
