import * as vscode from "vscode";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import * as path from "node:path";
import * as fs from "node:fs";

const execFileP = promisify(execFile);

const PY_CANDIDATES = ["python", "python3", "py"];

async function detectPython(): Promise<string | undefined> {
    const cfg = vscode.workspace.getConfiguration("recall").get<string>("pythonPath");
    if (cfg && fs.existsSync(cfg)) return cfg;

    // Prefer the active Python extension's interpreter if available
    try {
        const pyExt = vscode.extensions.getExtension("ms-python.python");
        if (pyExt) {
            await pyExt.activate();
            const api: any = pyExt.exports;
            const env = api?.environments?.getActiveEnvironmentPath?.();
            if (env?.path && fs.existsSync(env.path)) return env.path;
        }
    } catch { /* ignore */ }

    for (const c of PY_CANDIDATES) {
        try {
            await execFileP(c, ["--version"]);
            return c;
        } catch { /* try next */ }
    }
    return undefined;
}

async function hasRecall(python: string): Promise<boolean> {
    try {
        await execFileP(python, ["-c", "import recall, sys; sys.exit(0)"]);
        return true;
    } catch {
        return false;
    }
}

async function ensureInstalled(python: string): Promise<boolean> {
    if (await hasRecall(python)) return true;
    const pick = await vscode.window.showInformationMessage(
        "recall MCP server is not installed in this Python environment. Install now via pip?",
        "Install", "Cancel"
    );
    if (pick !== "Install") return false;
    return await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Installing recall-mcp" },
        async () => {
            try {
                await execFileP(python, ["-m", "pip", "install", "--upgrade", "recall-mcp"]);
                vscode.window.showInformationMessage("recall-mcp installed.");
                return true;
            } catch (e: any) {
                vscode.window.showErrorMessage(`pip install failed: ${e?.message ?? e}`);
                return false;
            }
        }
    );
}

class RecallProvider implements vscode.McpServerDefinitionProvider {
    private _onDidChange = new vscode.EventEmitter<void>();
    readonly onDidChangeMcpServerDefinitions = this._onDidChange.event;

    async provideMcpServerDefinitions(): Promise<vscode.McpServerDefinition[]> {
        const python = await detectPython();
        if (!python) {
            vscode.window.showWarningMessage(
                "recall: could not find a Python interpreter. Set 'recall.pythonPath' in settings."
            );
            return [];
        }
        if (!(await ensureInstalled(python))) return [];

        const ws = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        const cfgDb = vscode.workspace.getConfiguration("recall").get<string>("dbPath");
        const dbPath =
            cfgDb && cfgDb.length > 0
                ? cfgDb
                : ws
                    ? path.join(ws, ".recall", "recall.db")
                    : path.join(process.env.HOME ?? process.env.USERPROFILE ?? ".", ".recall", "recall.db");

        const def = new vscode.McpStdioServerDefinition(
            "recall",
            python,
            ["-m", "recall.server"],
            { RECALL_DB: dbPath }
        );
        return [def];
    }
}

export async function activate(context: vscode.ExtensionContext) {
    const provider = new RecallProvider();
    context.subscriptions.push(
        vscode.lm.registerMcpServerDefinitionProvider("recall.mcpProvider", provider)
    );

    context.subscriptions.push(
        vscode.commands.registerCommand("recall.checkInstall", async () => {
            const py = await detectPython();
            if (!py) { vscode.window.showErrorMessage("No Python found."); return; }
            const ok = await hasRecall(py);
            vscode.window.showInformationMessage(
                `Python: ${py} — recall ${ok ? "installed ✓" : "NOT installed"}`
            );
        }),
        vscode.commands.registerCommand("recall.installPip", async () => {
            const py = await detectPython();
            if (!py) { vscode.window.showErrorMessage("No Python found."); return; }
            await ensureInstalled(py);
        })
    );
}

export function deactivate() { /* nothing */ }
