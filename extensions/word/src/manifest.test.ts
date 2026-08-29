import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const root = process.cwd();

describe("Word add-in manifest", () => {
  it("declares a valid Word task-pane contract and existing HTTPS assets", async () => {
    const xml = await readFile(resolve(root, "manifest.xml"), "utf8");
    const document = new DOMParser().parseFromString(xml, "application/xml");

    expect(document.querySelector("parsererror")).toBeNull();
    expect(document.querySelector("OfficeApp")?.getAttribute("xsi:type")).toBe("TaskPaneApp");
    expect(document.querySelector("Id")?.textContent).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
    );
    expect(document.querySelector('Host[Name="Document"]')).not.toBeNull();
    expect(document.querySelector("Permissions")?.textContent).toBe("ReadWriteDocument");
    expect(document.querySelector('Set[Name="WordApi"]')?.getAttribute("MinVersion")).toBe("1.1");

    const source = document.querySelector("SourceLocation")?.getAttribute("DefaultValue");
    const icon = document.querySelector("IconUrl")?.getAttribute("DefaultValue");
    const highResolutionIcon = document
      .querySelector("HighResolutionIconUrl")
      ?.getAttribute("DefaultValue");
    expect(source).toBe("https://localhost:44317/taskpane.html");
    expect(icon).toBe("https://localhost:44317/icons/fieldledger-32.png");
    expect(highResolutionIcon).toBe("https://localhost:44317/icons/fieldledger-80.png");

    await expect(readFile(resolve(root, "public/icons/fieldledger-32.png"))).resolves.not.toHaveLength(0);
    await expect(readFile(resolve(root, "public/icons/fieldledger-80.png"))).resolves.not.toHaveLength(0);
  });
});
