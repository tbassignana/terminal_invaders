#!/bin/bash
#
# Setup Script for Terminal Invaders
# Creates the 'invaders' command for easy game launching
#
# Usage: ./setup.sh
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}==================================${NC}"
echo -e "${BLUE}  Terminal Invaders Setup${NC}"
echo -e "${BLUE}==================================${NC}"
echo ""

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GAME_FILE="${SCRIPT_DIR}/invaders.py"

# Check if the game file exists
if [ ! -f "$GAME_FILE" ]; then
    echo -e "${RED}Error: Cannot find invaders.py in ${SCRIPT_DIR}${NC}"
    exit 1
fi

echo -e "${GREEN}Found game file:${NC} ${GAME_FILE}"

# Make the Python file executable
chmod +x "$GAME_FILE"
echo -e "${GREEN}Made game file executable${NC}"

# Detect shell configuration file
SHELL_NAME=$(basename "$SHELL")
SHELL_RC=""

case "$SHELL_NAME" in
    zsh)
        SHELL_RC="$HOME/.zshrc"
        ;;
    bash)
        # On macOS, bash uses .bash_profile for login shells
        if [ -f "$HOME/.bash_profile" ]; then
            SHELL_RC="$HOME/.bash_profile"
        else
            SHELL_RC="$HOME/.bashrc"
        fi
        ;;
    *)
        echo -e "${YELLOW}Warning: Unsupported shell '$SHELL_NAME'. Using .zshrc${NC}"
        SHELL_RC="$HOME/.zshrc"
        ;;
esac

echo -e "${GREEN}Detected shell:${NC} ${SHELL_NAME}"
echo -e "${GREEN}Config file:${NC} ${SHELL_RC}"

# Check if the function already exists
if grep -q "function invaders()" "$SHELL_RC" 2>/dev/null; then
    echo -e "${YELLOW}The 'invaders' command already exists in ${SHELL_RC}${NC}"
    echo -e "${YELLOW}Removing old definition...${NC}"
    # Remove old function (handling multiline)
    sed -i.bak '/function invaders()/,/^}/d' "$SHELL_RC"
fi

# Append the invaders function to the shell config
echo "" >> "$SHELL_RC"
echo "# Terminal Invaders game command" >> "$SHELL_RC"
echo "function invaders() {" >> "$SHELL_RC"
echo "    python3 \"${GAME_FILE}\"" >> "$SHELL_RC"
echo "}" >> "$SHELL_RC"

echo ""
echo -e "${GREEN}Successfully added 'invaders' command to ${SHELL_RC}${NC}"
echo ""
echo -e "${BLUE}==================================${NC}"
echo -e "${BLUE}  Setup Complete!${NC}"
echo -e "${BLUE}==================================${NC}"
echo ""
echo -e "To activate the command, run one of the following:"
echo ""
echo -e "  ${YELLOW}source ${SHELL_RC}${NC}"
echo ""
echo -e "Or simply open a new terminal window."
echo ""
echo -e "Then type ${GREEN}invaders${NC} to start the game!"
echo ""

# Optional: Check for soundtrack
SOUNDTRACK="$HOME/soundtrack.mp3"
if [ -f "$SOUNDTRACK" ]; then
    echo -e "${GREEN}Soundtrack found:${NC} ${SOUNDTRACK}"
else
    echo -e "${YELLOW}Note: No soundtrack found at ${SOUNDTRACK}${NC}"
    echo -e "${YELLOW}The game will work without audio.${NC}"
    echo -e "${YELLOW}To enable music, place an MP3 file at ~/soundtrack.mp3${NC}"
fi

echo ""
echo -e "${GREEN}Enjoy the game!${NC}"
