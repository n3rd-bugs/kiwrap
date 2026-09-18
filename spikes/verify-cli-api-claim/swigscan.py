import pcbnew, re
print("pcbnew", pcbnew.GetBuildVersion())
names = dir(pcbnew)
pat = re.compile(r'(?i)netlist|specctra|dsn|FootprintLoad|FootprintEnumerate|LoadFootprint|GetLibraryFootprint|RunDRC|WriteDRC|DRC_ENGINE|DRC_TOOL|ERC')
print(sorted(n for n in names if pat.search(n)))
