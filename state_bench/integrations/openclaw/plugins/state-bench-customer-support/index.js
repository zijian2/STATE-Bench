import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

// Tool schemas from STATE-Bench customer support domain
const TOOL_SCHEMAS = [
  {
    name: "get_order",
    description: "Retrieve full details of an order by its order ID. Returns the order, all items in the order, and product details for each item.",
    parameters: {
      type: "object",
      properties: {
        order_id: { type: "string", description: "The order ID (e.g. 'ORD-5001')" },
      },
      required: ["order_id"],
      additionalProperties: false,
    },
  },
  {
    name: "get_customer",
    description: "Retrieve customer profile including membership tier, store credit balance, and preference settings.",
    parameters: {
      type: "object",
      properties: {
        customer_id: { type: "string", description: "The customer ID (e.g. 'cust_001')" },
      },
      required: ["customer_id"],
      additionalProperties: false,
    },
  },
  {
    name: "search_products",
    description: "Search the task-local product catalog by free-text query. Returns ranked compact matches with product_id, name, matched_field, category, subcategory, and in_stock. Call get_product_details with a returned product_id for price, stock, warranty, and return-window details.",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "Free-text product search query, such as 'large shirt'" },
      },
      required: ["query"],
      additionalProperties: false,
    },
  },
  {
    name: "get_product_details",
    description: "Retrieve full product details by exact product ID, including current price, stock status, warranty terms, and return window. Use search_products first when you do not know the product ID.",
    parameters: {
      type: "object",
      properties: {
        product_id: { type: "string", description: "The product ID (e.g. 'PROD-1001')" },
      },
      required: ["product_id"],
      additionalProperties: false,
    },
  },
  {
    name: "get_policies",
    description: "Look up the company's policies for a given topic. IMPORTANT: You must call this before using any write tool (process_return, process_refund, cancel_order, process_exchange, process_warranty_claim). The system will reject write operations if the relevant policy has not been reviewed first.",
    parameters: {
      type: "object",
      properties: {
        topic: {
          type: "string",
          enum: ["return", "refund", "cancellation", "exchange", "warranty", "shipping"],
          description: "The policy topic to look up",
        },
      },
      required: ["topic"],
      additionalProperties: false,
    },
  },
  {
    name: "get_warranty_status",
    description: "Check the warranty status for a specific item. Returns warranty type, coverage period, claim history, and current eligibility.",
    parameters: {
      type: "object",
      properties: {
        item_id: { type: "string", description: "The order item ID (e.g. 'ITEM-8001')" },
      },
      required: ["item_id"],
      additionalProperties: false,
    },
  },
  {
    name: "process_return",
    description: "Process a return for an order item. Two-step operation: first call without confirm to preview the return (eligibility, fees, component breakdown), then call again with confirm=true AND amount=<computed net refund> to execute. On confirm, you must compute the net refund yourself from the preview's components (restocking_fee, restocking_discount, shipping_clawback, bulk_clawback, repeat_surcharge, paid_return_shipping_fee) and pass it as `amount`. The env writes whatever amount you submit. Requires prior call to get_policies(topic='return').",
    parameters: {
      type: "object",
      properties: {
        item_id: { type: "string", description: "The order item ID to return" },
        reason: {
          type: "string",
          enum: ["defective", "wrong_item", "not_as_described", "changed_mind", "damaged_in_transit", "missing"],
          description: "Reason for the return",
        },
        amount: {
          type: "integer",
          description: "Net refund amount in dollars (required on confirm; ignored on preview). Compute from the preview's component breakdown: base item price minus restocking_fee plus restocking_discount minus shipping_clawback minus bulk_clawback minus repeat_surcharge minus paid_return_shipping_fee. The env writes this value verbatim to item.refund_amount; getting it wrong is a scoring failure.",
        },
        confirm: {
          type: "boolean",
          description: "Set to true to execute the return after previewing. Omit or set false to preview only.",
        },
      },
      required: ["item_id", "reason"],
      additionalProperties: false,
    },
  },
  {
    name: "process_refund",
    description: "Process a refund for an order item. Two-step operation: first call without confirm to preview the refund calculation, then call with confirm=true to execute. Requires prior call to get_policies(topic='refund').",
    parameters: {
      type: "object",
      properties: {
        item_id: { type: "string", description: "The order item ID to refund" },
        refund_method: {
          type: "string",
          enum: ["original_payment", "store_credit"],
          description: "How to issue the refund",
        },
        amount: {
          type: "integer",
          description: "Refund amount in dollars. Must match the policy-computed amount.",
        },
        confirm: {
          type: "boolean",
          description: "Set to true to execute the refund after previewing. Omit or set false to preview only.",
        },
      },
      required: ["item_id", "refund_method", "amount"],
      additionalProperties: false,
    },
  },
  {
    name: "cancel_order",
    description: "Cancel an order or specific items within an order. Two-step operation: first call without confirm to preview cancellation (fees, refund), then call with confirm=true to execute. Pass item_ids to cancel specific items, or omit to cancel the entire order. Requires prior call to get_policies(topic='cancellation').",
    parameters: {
      type: "object",
      properties: {
        order_id: { type: "string", description: "The order ID to cancel" },
        item_ids: {
          type: "array",
          items: { type: "string" },
          description: "Specific item IDs to cancel. Omit to cancel entire order.",
        },
        confirm: {
          type: "boolean",
          description: "Set to true to execute the cancellation after previewing.",
        },
      },
      required: ["order_id"],
      additionalProperties: false,
    },
  },
  {
    name: "process_exchange",
    description: "Exchange an item for a different product or variant. Two-step operation: first call without confirm to preview (price difference, eligibility), then call with confirm=true to execute. Requires prior call to get_policies(topic='exchange').",
    parameters: {
      type: "object",
      properties: {
        item_id: { type: "string", description: "The order item ID to exchange" },
        new_product_id: {
          type: "string",
          description: "The product ID for the replacement item",
        },
        confirm: {
          type: "boolean",
          description: "Set to true to execute the exchange after previewing.",
        },
      },
      required: ["item_id", "new_product_id"],
      additionalProperties: false,
    },
  },
  {
    name: "process_warranty_claim",
    description: "File a warranty claim for a defective item. Two-step operation: first call without confirm to preview (eligibility, resolution type, cost), then call with confirm=true to execute. Requires prior call to get_policies(topic='warranty').",
    parameters: {
      type: "object",
      properties: {
        warranty_id: { type: "string", description: "The warranty ID (e.g. 'WRT-3001')" },
        item_id: { type: "string", description: "The order item ID with the defect" },
        issue_description: {
          type: "string",
          description: "Description of the defect or issue",
        },
        confirm: {
          type: "boolean",
          description: "Set to true to execute the warranty claim after previewing.",
        },
      },
      required: ["warranty_id", "item_id", "issue_description"],
      additionalProperties: false,
    },
  },
];

async function callToolServer(toolName, args, serverUrl) {
  try {
    const response = await fetch(`${serverUrl}/execute_tool`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool_name: toolName, arguments: args }),
    });

    if (!response.ok) {
      const errorText = await response.text();
      return {
        content: [{ type: "text", text: `Error: HTTP ${response.status} - ${errorText}` }],
        details: { error: true, status: response.status },
      };
    }

    const result = await response.json();
    return result;
  } catch (error) {
    return {
      content: [{ type: "text", text: `Error calling tool server: ${error.message}` }],
      details: { error: true, message: error.message },
    };
  }
}

export default definePluginEntry({
  id: "state-bench-customer-support",
  name: "STATE-Bench Customer Support Tools",
  description: "Customer support domain tools for STATE-Bench evaluation",

  register(api) {
    // Default tool server URL (matches state-bench-travel plugin)
    const serverUrl = "http://127.0.0.1:8765";

    for (const schema of TOOL_SCHEMAS) {
      api.registerTool({
        name: schema.name,
        description: schema.description,
        parameters: schema.parameters,
        async execute(_id, params) {
          return await callToolServer(schema.name, params || {}, serverUrl);
        },
      });
    }
  },
});
