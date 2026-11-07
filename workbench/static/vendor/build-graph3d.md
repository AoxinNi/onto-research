# graph3d.js

Single ESM bundle of 3d-force-graph 1.80.1 + three 0.186.1 (with CSS2DRenderer and UnrealBloomPass),
vendored so the workbench works offline. Rebuild:

    npm install 3d-force-graph@1.80.1 three@0.186.1 esbuild
    cat > entry.js <<'JS'
    export { default as ForceGraph3D } from "3d-force-graph";
    export * as THREE from "three";
    export { CSS2DRenderer, CSS2DObject } from "three/examples/jsm/renderers/CSS2DRenderer.js";
    export { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";
    JS
    npx esbuild entry.js --bundle --minify --format=esm --legal-comments=none --outfile=graph3d.js

fonts/: Geist and Geist Mono variable fonts (SIL Open Font License 1.1), from the `geist` npm package.
