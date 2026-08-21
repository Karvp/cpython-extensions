"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

function sha256(buffer) {
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

function toBuffer(data) {
  if (Buffer.isBuffer(data)) {
    return data;
  }
  if (data instanceof ArrayBuffer) {
    return Buffer.from(data);
  }
  if (ArrayBuffer.isView(data)) {
    return Buffer.from(data.buffer, data.byteOffset, data.byteLength);
  }
  if (typeof data === "string") {
    return Buffer.from(data, "binary");
  }
  throw new TypeError(`unsupported release asset response type: ${typeof data}`);
}

async function remoteAssetSha256({ github, owner, repo, asset }) {
  if (typeof asset.digest === "string" && asset.digest.startsWith("sha256:")) {
    return asset.digest.slice("sha256:".length).toLowerCase();
  }

  const response = await github.request(
    "GET /repos/{owner}/{repo}/releases/assets/{asset_id}",
    {
      owner,
      repo,
      asset_id: asset.id,
      headers: { accept: "application/octet-stream" },
    },
  );
  return sha256(toBuffer(response.data));
}

async function uploadAsset({ github, owner, repo, releaseId, filePath, core }) {
  const data = fs.readFileSync(filePath);
  const name = path.basename(filePath);
  core.info(`uploading certified release asset: ${name}`);
  await github.rest.repos.uploadReleaseAsset({
    owner,
    repo,
    release_id: releaseId,
    name,
    data,
    headers: {
      "content-type": "application/octet-stream",
      "content-length": data.length,
    },
  });
}

module.exports = async function publishRelease({ github, context, core }) {
  const { owner, repo } = context.repo;
  const tag = process.env.RELEASE_TAG;
  const version = process.env.RELEASE_VERSION;
  const channel = process.env.RELEASE_CHANNEL;
  const releaseDir = path.resolve(process.env.RELEASE_DIR || "dist");

  if (!tag || !version || !channel) {
    throw new Error("RELEASE_TAG, RELEASE_VERSION, and RELEASE_CHANNEL are required");
  }
  if (channel !== "stable" && channel !== "preview") {
    throw new Error(`unsupported release channel: ${channel}`);
  }
  if (!fs.statSync(releaseDir).isDirectory()) {
    throw new Error(`release directory not found: ${releaseDir}`);
  }

  const prerelease = channel === "preview";
  let title = `cpython-extensions ${version}`;
  if (prerelease) {
    const prefix = `v${version}-`;
    const label = tag.startsWith(prefix) ? tag.slice(prefix.length) : "preview";
    title += ` (${label})`;
  }

  const expectedFiles = fs
    .readdirSync(releaseDir)
    .filter((name) => fs.statSync(path.join(releaseDir, name)).isFile())
    .sort();
  const distributionFiles = expectedFiles.filter(
    (name) => name.endsWith(".whl") || name.endsWith(".tar.gz"),
  );
  if (distributionFiles.length !== 2 || !expectedFiles.includes("SHA256SUMS.txt")) {
    throw new Error(
      `unexpected certified release contents: ${expectedFiles.join(", ")}`,
    );
  }

  let release;
  try {
    release = (
      await github.rest.repos.getReleaseByTag({ owner, repo, tag })
    ).data;
    core.info(`found existing GitHub Release id=${release.id} for ${tag}`);
  } catch (error) {
    if (error.status !== 404) {
      throw error;
    }
    core.info(`creating ${prerelease ? "pre" : ""}release for ${tag}`);
    release = (
      await github.rest.repos.createRelease({
        owner,
        repo,
        tag_name: tag,
        name: title,
        draft: false,
        prerelease,
        generate_release_notes: true,
      })
    ).data;
  }

  // A previous interrupted workflow may have created a draft or created a
  // preview tag as a normal release. Normalize those states on rerun.
  if (
    release.draft ||
    release.prerelease !== prerelease ||
    release.name !== title
  ) {
    release = (
      await github.rest.repos.updateRelease({
        owner,
        repo,
        release_id: release.id,
        name: title,
        draft: false,
        prerelease,
      })
    ).data;
    core.info("normalized existing release metadata");
  }

  const assets = await github.paginate(github.rest.repos.listReleaseAssets, {
    owner,
    repo,
    release_id: release.id,
    per_page: 100,
  });
  const assetsByName = new Map(assets.map((asset) => [asset.name, asset]));

  for (const name of expectedFiles) {
    const filePath = path.join(releaseDir, name);
    const localData = fs.readFileSync(filePath);
    const localDigest = sha256(localData);
    const existing = assetsByName.get(name);

    if (!existing) {
      await uploadAsset({
        github,
        owner,
        repo,
        releaseId: release.id,
        filePath,
        core,
      });
      continue;
    }

    const remoteDigest = await remoteAssetSha256({
      github,
      owner,
      repo,
      asset: existing,
    });
    if (remoteDigest === localDigest) {
      core.info(`existing asset matches certified bytes: ${name}`);
      continue;
    }

    if (!prerelease) {
      throw new Error(
        `published stable release asset differs from certified bytes: ${name}; ` +
          "do not overwrite a stable release—publish a new package version",
      );
    }

    // Preview releases are explicitly release-pipeline rehearsals. Repair a
    // stale/partial preview asset so rerunning the same tag is self-healing.
    core.warning(`replacing stale preview asset: ${name}`);
    await github.rest.repos.deleteReleaseAsset({
      owner,
      repo,
      asset_id: existing.id,
    });
    await uploadAsset({
      github,
      owner,
      repo,
      releaseId: release.id,
      filePath,
      core,
    });
  }

  core.info(
    `GitHub ${prerelease ? "prerelease" : "release"} ready: ${release.html_url}`,
  );
};
