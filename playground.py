#!/usr/bin/env -S ipython -i

from whenever import Instant

# 30d ago
thirty_days_ago = Instant.now().to_system_tz().add(days=-30)