#!/bin/sh

snap --nosplash --nogui --modules --update-all 2>&1 | while read -r line; do
    echo "$line"
    if [ "$line" = "updates=0" ]; then
        echo "✅ No updates found, exiting SNAP safely."
        sleep 2
        pgrep -f "snap/jre/bin/java" | xargs -r kill -TERM
    fi
done
