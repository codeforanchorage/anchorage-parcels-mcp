# Built-in Plugins Reference

OpenContext includes built-in plugins for CKAN and Socrata open data portals, plus Anchorage-specific GIS and parcel plugins that are forkable to other cities.

## CKAN Plugin

For CKAN-based open data portals (e.g., data.boston.gov, data.gov, data.gov.uk).

### Configuration

```yaml
plugins:
  ckan:
    enabled: true
    base_url: "https://data.yourcity.gov"       # CKAN API base URL
    portal_url: "https://data.yourcity.gov"     # Public portal URL
    city_name: "Your City"                      # City/organization name
    timeout: 120                                # HTTP timeout in seconds
    api_key: "${CKAN_API_KEY}"                  # Optional: API key
```

### Tools

- `ckan__search_datasets(query, limit)` - Search for datasets
- `ckan__get_dataset(dataset_id)` - Get dataset metadata
- `ckan__query_data(resource_id, filters, limit)` - Query data from a resource
- `ckan__get_schema(resource_id)` - Get schema for a resource

### Examples

**Search datasets:**
```
Search for datasets about housing in Boston
```

**Get dataset:**
```
Get details about the "311 Service Requests" dataset
```

**Query data:**
```
Query the first 10 records from resource abc123
```

## CKAN API

This plugin uses CKAN's Action API:
- `/api/3/action/package_search` - Search datasets
- `/api/3/action/package_show` - Get dataset
- `/api/3/action/datastore_search` - Query data

See [CKAN API documentation](https://docs.ckan.org/en/latest/api/) for details.

## Socrata Plugin

For Socrata-based open data portals (e.g., data.cityofchicago.org, data.cityofnewyork.us, data.seattle.gov).

**Note:** Socrata requires a free app token. Register at [https://dev.socrata.com/register](https://dev.socrata.com/register).

### Configuration

```yaml
plugins:
  socrata:
    enabled: true
    base_url: "https://data.cityofboston.gov"
    portal_url: "https://data.cityofboston.gov"
    city_name: "Boston"
    app_token: "${SOCRATA_APP_TOKEN}"   # Required
    timeout: 30.0                        # HTTP timeout (default: 30)
```

### Tools

- `socrata__search_datasets(query, limit)` - Search for datasets in the portal catalog
- `socrata__get_dataset(dataset_id)` - Get full metadata for a dataset (4x4 ID)
- `socrata__get_schema(dataset_id)` - Get column schema for constructing SoQL queries
- `socrata__query_dataset(dataset_id, soql_query)` - Query data using SoQL
- `socrata__execute_sql(dataset_id, soql)` - Execute raw SoQL query (advanced, similar to CKAN execute_sql)
- `socrata__list_categories()` - List all categories with dataset counts

### Examples

**Search datasets:**
```
Search for datasets about housing in Boston
```

**Get dataset:**
```
Get details about dataset wc4w-4jew
```

**Get schema (call before query_dataset):**
```
Get schema for dataset wc4w-4jew
```

**Query data:**
```
Query dataset wc4w-4jew with: SELECT * WHERE year > 2020 LIMIT 50
```

**List categories:**
```
List all dataset categories on Boston's open data portal
```

### Socrata API

This plugin uses two Socrata API layers:
- **Discovery API** (api.us.socrata.com) - Catalog search, categories
- **SODA3** (portal domain) - Dataset metadata, schema, data queries

See [Socrata developer documentation](https://dev.socrata.com/) for details.

## Anchorage Parcels Plugin

Domain tools over the Municipality of Anchorage property/assessment records (the public MOA PropertyInformation Feature Layer, plus Addresses and Subdivisions). Read-only; the schema is baked into the tools so no schema pre-flight is needed. Forkable to any city whose assessor publishes a parcel Feature Layer (layer URLs + one field-map block).

### Configuration

```yaml
plugins:
  anchorage_parcels:
    enabled: true
    # Defaults point at the MOA layers; override to fork for your city
    # property_layer_url: "https://services2.arcgis.com/<org>/arcgis/rest/services/<Property>/FeatureServer/0"
    # addresses_layer_url: "..."
    # subdivisions_layer_url: "..."
    city_name: "Municipality of Anchorage"
    field_map: {} # logical->physical field-name overrides
    timeout: 30
```

### Tools

- `anchorage_parcels__find_parcel(parcel_id, category, out_fields, limit)` - Parcel lookup across all four MOA ID formats, with fuzzy fallback
- `anchorage_parcels__get_parcel_details(parcel_id)` - Full assessment record (owner, valuation history, exemptions, deed) + assessor Datalet link
- `anchorage_parcels__search_by_owner(name, limit, category)` - Owner-name search, ordered by appraised value
- `anchorage_parcels__search_by_address(address, limit)` - Situs search with address-point/point-in-polygon fallback
- `anchorage_parcels__parcels_at_point(lat, lon)` - Point-in-polygon across Parcel/Lease/Economic categories
- `anchorage_parcels__query_parcels(where, out_fields, category, limit, offset, order_by)` - Validated SQL escape hatch with pagination
- `anchorage_parcels__parcel_stats(stat_type, stat_field, group_by, where, category, percentile)` - Aggregates incl. median via percentile_cont

See [PARCELS.md](PARCELS.md) for the full reference, fork-for-your-city instructions, and deployment steps.

## Custom Plugins

If your portal doesn't use CKAN, you can create a custom plugin. See [Custom Plugins Guide](CUSTOM_PLUGINS.md) for instructions.

## Examples

See [examples/](../examples/) for complete configuration examples.
