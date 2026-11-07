# Publish on GitHub from PowerShell

1. Extract this ZIP and open its folder in VS Code. Do not upload the ZIP itself as the repository content.
2. Create an **empty** public GitHub repository (no README/license/gitignore initialization).
3. In the extracted project root, run:

```powershell
git init
git branch -M main
git status
git add .
git commit -m "Initial TwirlyTunes research prototype"
git remote add origin https://github.com/YOUR-USERNAME/TwirlyTunes-AI-Baby-Soothing-System.git
git push -u origin main
```

Replace `YOUR-USERNAME` with your GitHub username. Sign in if prompted.

For later changes:

```powershell
git status
git add .
git commit -m "Describe your actual change"
git push
```

To invite coauthors: repository Settings → Collaborators. For collaborative work, use branches and pull requests. Do not commit credentials or private audio recordings.
